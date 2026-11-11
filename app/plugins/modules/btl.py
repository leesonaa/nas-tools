import base64
import hashlib
import json
import re
import time
from html import unescape
from http.cookies import CookieError, SimpleCookie
from threading import Lock
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bencode import BencodeDecodeError
try:
    from bencode import bencode, bdecode
except ImportError:
    from bencode import encode as bencode, decode as bdecode

from app.indexer.indexerConf import IndexerConf
from app.plugins.modules._base import _IPluginModule
from app.sites import Sites
from app.utils import RequestUtils, ExceptionUtils, StringUtils
from config import Config


class Btl(_IPluginModule):
    module_name = "不太灵影视"
    module_desc = "让内建索引器支持检索不太灵影视资源（支持全分页磁力链接）"
    module_icon = "btl.png"
    module_color = "#0EA5E9"
    module_version = "0.4"
    module_author = "leeson"
    author_url = ""
    module_config_prefix = "btl_"
    module_order = 21
    auth_level = 1

    _enable = False
    _domain = "https://web5.mukaku.com"
    # 列表可能不再直接提供 zlink，优先使用磁力按钮接口，失败时尝试通过种子生成。
    _tlist_url = "https://web5.mukaku.com/prod/api/v1/getTList"
    _app_id = "83768d9ad4"
    _identity = "23734adac0301bccdcb107c4aa21f96c"
    # 站点分类：1=电影 2=电视剧，getTList 不支持关键字搜索，只能挨个分类翻页后本地匹配标题
    _tlist_categories = (1, 2)
    # 站点固定每页返回 20 条，total 固定 400，即每个分类最多 20 页
    _tlist_page_size = 20
    # 命中目标集后额外翻页，避免同剧资源分散在相邻分页而漏搜
    _tlist_pages_after_match = 5
    # 连接池大小，翻页请求按顺序执行以便命中后完成有限页数的补充检索
    _tlist_concurrency = 5
    # 接口允许超出 total 继续翻页，设置上限避免站点异常时无限请求
    _tlist_max_pages = 500
    _ua = None
    _cookie = ""
    _custom_ua = ""
    # 复用同一个连接池（keep-alive），省掉重复握手开销
    _session = None
    # ddddocr 实例：初始化要加载 ONNX 模型，开销大，复用同一个实例
    _ocr = None
    # ddddocr 不可用（未安装/初始化失败）时置位，避免每次翻页都刷一遍错误日志
    _ocr_unavailable = False
    # 验证码识别错误时重新获取验证码重试的最大次数
    _captcha_max_attempts = 3
    # 一轮识别全部失败后的冷却截止时间（秒级时间戳），冷却期内不再重复尝试，避免对站点狂刷
    _captcha_block_until = 0

    @staticmethod
    def get_fields():
        return [
            {
                "type": "div",
                "content": [
                    [
                        {
                            "title": "站点地址",
                            "required": "required",
                            "type": "text",
                            "content": [
                                {"id": "domain", "placeholder": "https://web5.mukaku.com"}
                            ],
                        }
                    ],
                    [
                        {
                            "title": "列表接口地址",
                            "required": "required",
                            "tooltip": "一般不用修改，站点更换域名时需要同步修改",
                            "type": "text",
                            "content": [
                                {"id": "tlist_url", "placeholder": "https://web5.mukaku.com/prod/api/v1/getTList"}
                            ],
                        }
                    ],
                    [
                        {
                            "title": "Cookie",
                            "required": "",
                            "tooltip": "可选，站点请求使用的 Cookie",
                            "type": "text",
                            "content": [
                                {"id": "cookie", "placeholder": "站点 Cookie"}
                            ],
                        }
                    ],
                    [
                        {
                            "title": "User-Agent",
                            "required": "",
                            "tooltip": "需与获取 Cookie 的浏览器 UA 一致，留空使用系统默认",
                            "type": "text",
                            "content": [
                                {"id": "custom_ua", "placeholder": "获取 Cookie 的浏览器 UA"}
                            ],
                        }
                    ],
                    [
                        {
                            "title": "请求连接数",
                            "required": "",
                            "tooltip": "HTTP 请求连接池大小，默认5",
                            "type": "text",
                            "content": [
                                {"id": "tlist_concurrency", "placeholder": "5"}
                            ],
                        }
                    ],
                    [
                        {
                            "title": "启用",
                            "required": "",
                            "type": "switch",
                            "id": "enable",
                        }
                    ],
                ],
            }
        ]

    def init_config(self, config=None):
        if config:
            self._domain = (config.get("domain") or self._domain).rstrip("/")
            self._tlist_url = config.get("tlist_url") or self._tlist_url
            try:
                self._tlist_concurrency = max(1, int(config.get("tlist_concurrency") or self._tlist_concurrency))
            except (TypeError, ValueError):
                pass
            self._enable = config.get("enable")
            self._cookie = (config.get("cookie") or "").strip()
            self._custom_ua = (config.get("custom_ua") or "").strip()
        self._ua = self._custom_ua or Config().get_ua()
        self._session = requests.Session()
        self._request_lock = Lock()
        self._ocr_unavailable = False
        if self._cookie:
            host = urlparse(self._tlist_url).hostname
            if not host:
                self.error(f"【{self.module_name}】列表接口地址无效，无法设置 Cookie")
            else:
                for part in self._cookie.split(";"):
                    name, sep, value = part.strip().partition("=")
                    if not sep or not name:
                        self.warn(f"【{self.module_name}】Cookie 格式无效，请使用名称=值的格式")
                        continue
                    self._session.cookies.set(name.strip(), value.strip(), domain=host, path="/")
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self._tlist_concurrency, pool_maxsize=self._tlist_concurrency
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        if self._enable:
            self.__register_site_with_proxy()

    def get_state(self):
        return self._enable

    def stop_service(self):
        pass

    def get_indexers(self):
        if not self._enable:
            return []
        return [
            IndexerConf(
                {
                    "id": "btl-plugin",
                    "name": "不太灵影视(插件)",
                    "domain": self._domain,
                    "public": True,
                    "builtin": False,
                    "proxy": True,
                    "parser": self.__class__.__name__,
                }
            )
        ]

    def __register_site_with_proxy(self):
        try:
            sites = Sites()
            if sites.get_sites_by_url_domain(self._domain):
                return
            host = urlparse(self._domain).netloc or self._domain
            sites.add_site(
                name=f"不太灵影视-{host}",
                site_pri=0,
                rssurl=self._domain + "/",
                note=json.dumps({"proxy": "Y"}),
            )
            self.info(f"【{self.module_name}】已将 {self._domain} 注册为站点并开启代理")
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.error(f"【{self.module_name}】自动注册站点代理失败: {e}")

    def _headers(self):
        return {"User-Agent": self._ua, "Accept": "application/json"}

    @staticmethod
    def _proxies():
        return Config().get_proxies()

    @staticmethod
    def __normalize_title(title):
        title = re.sub(r"^\[(?:BT下载|BT|磁力下载|种子下载)\]\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(r"([.\s()[\]_-])(?:19\d{2}|20[0-4]\d)(?=$|[.\s()[\]_-])", r"\1", title)
        return re.sub(r"\.{2,}", ".", title).strip()

    @staticmethod
    def __parse_size(size):
        if not size:
            return 0
        match = re.search(r"[\d.]+\s*(?:TB|GB|MB|KB|B)", str(size), re.IGNORECASE)
        if not match:
            return 0
        try:
            return StringUtils.num_filesize(match.group(0))
        except Exception:
            return 0

    @staticmethod
    def __normalize_match_title(title):
        title = unescape(str(title or "")).casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", title)

    @staticmethod
    def __entry_matches_keyword(entry, keyword):
        keyword = Btl.__normalize_match_title(keyword)
        if not keyword:
            return False
        # getTList 没有 otitle/alias 字段，只能拿剧名(title)和资源文件名(zname)来匹配
        candidates = [entry.get("title"), entry.get("zname")]
        for candidate in candidates:
            candidate = Btl.__normalize_match_title(candidate)
            if not candidate:
                continue
            # 站点条目常按季拆分命名（如“XXX 第二季”），关键字为剧名不含季号，
            # 因此用包含匹配而非完全相等，避免分季条目被误判为“非同名”而漏搜
            if candidate == keyword or keyword in candidate or candidate in keyword:
                return True
        return False

    def search(self, indexer, keyword, page=0, filter_args=None):
        if not indexer or not keyword:
            return []

        target_season, target_episode = self.__get_target_episode(filter_args)
        entries = self.__collect_tlist_entries(keyword, target_season, target_episode)
        if not entries:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
            return []

        results = []
        seen_links = set()
        for entry in entries:
            if not self.__entry_matches_keyword(entry, keyword):
                continue
            enclosure = entry.get("zlink") or ""
            if not enclosure.lower().startswith("magnet:") or enclosure in seen_links:
                continue
            seen_links.add(enclosure)
            title = entry.get("zname") or entry.get("title") or ""
            if not title:
                continue
            results.append(
                {
                    "indexer_id": indexer.id,
                    "indexer": indexer.name,
                    "title": self.__normalize_title(title),
                    "enclosure": enclosure,
                    "description": entry.get("conta") or "",
                    "size": self.__parse_size(entry.get("zsize")),
                    "seeders": 0,
                    "peers": 0,
                    "freeleech": False,
                    "downloadvolumefactor": 1.0,
                    "uploadvolumefactor": 1.0,
                    "page_url": self._domain,
                    "imdbid": "",
                }
            )

        if results:
            self.warn(f"【{self.module_name}】{indexer.name} 共查询 {len(entries)} 条，返回磁力数据：{len(results)}")
        else:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
        return results

    @staticmethod
    def __get_target_episode(filter_args):
        """
        取订阅缺失的最早一集作为翻页停止目标：缺失从 E06 开始就只需翻到 E06，
        不必像补全第一季那样一直翻到 E01；filter_args 未指定季/集时默认按 S01E01 找起，
        找到该集（或覆盖该集的整季资源）即可停止翻页。
        """
        seasons = filter_args.get("season") if filter_args else None
        episodes = filter_args.get("episode") if filter_args else None
        target_season = min((s for s in seasons if s is not None), default=1) if seasons else 1
        target_episode = min((e for e in episodes if e is not None), default=1) if episodes else 1
        return target_season, target_episode

    @staticmethod
    def __is_target_episode(entry, keyword, season, episode):
        if not Btl.__entry_matches_keyword(entry, keyword):
            return False
        title = " ".join(str(entry.get(field) or "") for field in ("title", "zname"))
        if re.search(rf"(?<![A-Z0-9])S0*{season}[\s._-]*E0*{episode}(?![A-Z0-9])", title, re.IGNORECASE):
            return True
        if re.search(rf"(?<![A-Z0-9]){season}\s*[xX]\s*0*{episode}(?![A-Z0-9])", title):
            return True
        if season != 1:
            return False
        # 整季资源常按"第01-04集"这样的区间命名，而非单集，需判断目标集是否落在区间内
        for match in re.finditer(r"第\s*0*(\d+)(?:\s*[-~]\s*0*(\d+))?\s*[集话話]", title):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            if start <= episode <= end:
                return True
        return False

    def __collect_tlist_entries(self, keyword, target_season, target_episode):
        """
        getTList 不支持关键字搜索，只能把"电影"、"电视剧"两个分类逐页翻下去，
        再本地按标题匹配关键字；匹配到目标集数（默认 S01E01，或覆盖它的整季资源）后，
        再额外翻若干页，减少同剧资源分散在相邻分页时的漏搜。
        """
        entries = []
        finished_categories = set()
        page_signatures = {category: set() for category in self._tlist_categories}
        stop_after_page = None

        for page in range(1, self._tlist_max_pages + 1):
            for category in self._tlist_categories:
                if category in finished_categories:
                    continue
                page_list, _ = self.__get_tlist_page(category, page)
                if not page_list:
                    finished_categories.add(category)
                    continue

                signature = tuple(
                    entry.get("zlink") or entry.get("zname") or entry.get("title") or ""
                    for entry in page_list
                )
                if signature and signature in page_signatures[category]:
                    finished_categories.add(category)
                    continue
                if signature:
                    page_signatures[category].add(signature)
                for entry in page_list:
                    link = entry.get("zlink") or ""
                    if (self.__entry_matches_keyword(entry, keyword)
                            and (not isinstance(link, str) or not link.lower().startswith("magnet:"))):
                        entry["zlink"] = self.__get_magnet(entry)
                entries.extend(page_list)

                if stop_after_page is None and any(
                    self.__is_target_episode(entry, keyword, target_season, target_episode)
                    for entry in page_list
                ):
                    stop_after_page = page + self._tlist_pages_after_match

            if len(finished_categories) == len(self._tlist_categories):
                break
            if stop_after_page is not None and page >= stop_after_page:
                break

        return entries

    def __verify_javascript(self, response):
        original = urlparse(self._tlist_url)
        challenge = urlparse(response.url)
        if (challenge.scheme, challenge.hostname, challenge.port) != (
            original.scheme, original.hostname, original.port
        ):
            self.error(f"【{self.module_name}】站点验证地址不匹配")
            return False
        match = re.search(
            r'document\.cookie\s*=\s*(["\'])(ge_js_validator_\d+=[^"\']*)\1',
            response.text,
        )
        if not match or not re.search(r'window\.location\.reload\s*\(\s*\)', response.text):
            self.error(f"【{self.module_name}】不支持当前站点 JavaScript 验证页面")
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(match.group(2))
        except CookieError:
            self.error(f"【{self.module_name}】站点验证 Cookie 格式无效")
            return False
        if len(cookies) != 1:
            self.error(f"【{self.module_name}】站点验证 Cookie 格式无效")
            return False
        name, cookie = next(iter(cookies.items()))
        if (not re.fullmatch(r"ge_js_validator_\d+", name) or not cookie.value
                or cookie["domain"] or cookie["httponly"] or cookie["path"] not in ("", "/")):
            self.error(f"【{self.module_name}】站点验证 Cookie 属性不受支持")
            return False
        max_age = cookie["max-age"]
        if max_age and (not max_age.isdigit() or int(max_age) <= 0):
            self.error(f"【{self.module_name}】站点验证 Cookie 有效期无效")
            return False
        self._session.cookies.set(
            name, cookie.value, domain=challenge.hostname, path="/",
            secure=bool(cookie["secure"]), expires=int(time.time()) + int(max_age) if max_age else None,
        )
        return True

    def __get_ocr(self):
        """
        获取复用的 ddddocr 实例，不可用时返回 None（只在第一次失败时记录错误）
        """
        if self._ocr is not None:
            return self._ocr
        if self._ocr_unavailable:
            return None
        try:
            import ddddocr
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        except ImportError:
            self._ocr_unavailable = True
            self.error(f"【{self.module_name}】触发了图形验证码，但未安装 ddddocr 库，无法自动识别。请运行 pip install ddddocr")
        except Exception as e:
            self._ocr_unavailable = True
            self.error(f"【{self.module_name}】ddddocr 初始化失败: {e}")
        return self._ocr

    @staticmethod
    def __need_captcha(body):
        """
        判断接口返回是否为验证码拦截。只看状态字段和字段名，不看列表数据里的值，
        避免资源标题里恰好出现“验证码”/“captcha”被误判
        """
        if not isinstance(body, dict):
            return False
        data = body.get("data")
        # 正常的列表/磁力结果一定不是验证码拦截
        if isinstance(data, dict) and ("list" in data or "zlink" in data):
            return False
        if body.get("code") == 401:
            return True
        message = str(body.get("message") or body.get("msg") or "")
        # 站点实际返回的提示是“请验证后继续查看”，不含“验证码”三个字，所以匹配“验证”
        if "验证" in message or "captcha" in message.lower():
            return True
        keys = list(body.keys()) + (list(data.keys()) if isinstance(data, dict) else [])
        return any("captcha" in str(key).lower() for key in keys)

    def __recognize_captcha(self, client, ocr, captcha_url, captcha_params):
        """
        获取一张验证码图片并识别，失败返回空字符串
        """
        resp = client.get_res(url=captcha_url, params=captcha_params, allow_redirects=False)
        if resp is None or resp.status_code != 200:
            return ""
        try:
            data = resp.json()
            img_data = data.get("data") if isinstance(data, dict) else None
            if not img_data:
                return ""
            # 解析 base64 图片数据
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            img_bytes = base64.b64decode(img_data)
        except Exception:
            return ""
        try:
            return (ocr.classification(img_bytes) or "").strip()
        except Exception as e:
            self.error(f"【{self.module_name}】验证码 OCR 识别异常: {e}")
            return ""

    def __verify_captcha(self, client):
        """
        自动识别并绕过图文验证码（如连续查看 50 次触发的 getViewGateCaptcha 验证），
        识别错误时重新获取验证码重试，最多 _captcha_max_attempts 次
        """
        if time.time() < self._captcha_block_until:
            return False
        ocr = self.__get_ocr()
        if ocr is None:
            return False

        captcha_url = urljoin(self._tlist_url, "getViewGateCaptcha")
        verify_url = urljoin(self._tlist_url, "verifyGateCaptcha")
        captcha_params = {"app_id": self._app_id, "identity": self._identity}

        for attempt in range(1, self._captcha_max_attempts + 1):
            code = self.__recognize_captcha(client, ocr, captcha_url, captcha_params)
            if not code:
                continue
            verify_data = {"app_id": self._app_id, "identity": self._identity, "code": code}
            v_resp = client.post_res(url=verify_url, json=verify_data, allow_redirects=False)
            if v_resp is not None and v_resp.status_code == 200:
                try:
                    res_json = v_resp.json()
                except ValueError:
                    res_json = None
                if isinstance(res_json, dict) and res_json.get("success"):
                    self.info(f"【{self.module_name}】图形验证码自动识别并验证成功：{code}（第 {attempt} 次尝试）")
                    return True
            self.warn(f"【{self.module_name}】验证码识别结果 {code} 未通过验证（{attempt}/{self._captcha_max_attempts}）")
        self.error(f"【{self.module_name}】图形验证码自动识别失败，60 秒内不再重试")
        self._captcha_block_until = time.time() + 60
        return False

    def __get_response(self, url, params=None):
        client = RequestUtils(
            headers=self._headers(), proxies=self._proxies(), session=self._session, timeout=15
        )
        with self._request_lock:
            resp = client.get_res(url=url, params=params, allow_redirects=False)

            # 检测是否触发了图形验证码（通常返回 json 包含特定提示或特定状态，或者根据内容判断）
            if resp is not None and resp.status_code == 200:
                try:
                    # 不依赖 Content-Type（站点可能用 text/html 返回 JSON），
                    # 正文以 { 开头才按 JSON 解析；种子文件是二进制，不会进入这里
                    if resp.content.lstrip()[:1] == b"{":
                        body_json = resp.json()
                        if self.__need_captcha(body_json):
                            self.info(f"【{self.module_name}】站点要求验证：{body_json.get('message') or body_json.get('msg') or '未知提示'}，尝试自动识别验证码")
                            if self.__verify_captcha(client):
                                resp = client.get_res(url=url, params=params, allow_redirects=False)
                except Exception as e:
                    self.warn(f"【{self.module_name}】验证码处理异常: {e}")

            if (resp is not None and resp.status_code == 200
                    and "text/html" in resp.headers.get("Content-Type", "").lower()
                    and "ge_js_validator_" in resp.text):
                if not self.__verify_javascript(resp):
                    return None
                resp = client.get_res(url=url, params=params, allow_redirects=False)
                if (resp is not None and resp.status_code == 200
                        and "text/html" in resp.headers.get("Content-Type", "").lower()
                        and "ge_js_validator_" in resp.text):
                    self.error(f"【{self.module_name}】站点 JavaScript 验证后重试失败")
                    return None
        if resp is None:
            self.error(f"【{self.module_name}】站点请求失败")
            return None
        if resp.status_code != 200:
            self.error(f"【{self.module_name}】站点返回 HTTP {resp.status_code}")
            return None
        return resp

    def __get_magnet(self, entry):
        resource_id = entry.get("id")
        if resource_id:
            url = urljoin(self._tlist_url, "recordMagnetView")
            params = {"id": resource_id, "app_id": self._app_id, "identity": self._identity}
            resp = self.__get_response(url, params=params)
            if resp is not None:
                try:
                    data = resp.json()
                except requests.exceptions.JSONDecodeError:
                    self.error(f"【{self.module_name}】磁力接口返回的不是有效 JSON")
                else:
                    result = data.get("data") if isinstance(data, dict) and data.get("success") else None
                    magnet = result.get("zlink") if isinstance(result, dict) else None
                    if isinstance(magnet, str) and magnet.lower().startswith("magnet:"):
                        return magnet
                    message = data.get("message") if isinstance(data, dict) else None
                    self.error(f"【{self.module_name}】磁力接口未返回有效链接：{message or '未知错误'}")
                    if self.__need_captcha(data):
                        # 仍被验证拦截时，下载种子的接口同样会被拦，不再白白请求一次
                        return ""
        else:
            self.warn(f"【{self.module_name}】匹配的资源缺少 ID，尝试下载种子")
        return self.__download_magnet(entry)

    def __download_magnet(self, entry):
        down = entry.get("down")
        if not isinstance(down, str) or not down:
            self.warn(f"【{self.module_name}】匹配的资源缺少下载地址")
            return ""
        url = urljoin(self._tlist_url, down)
        original, target = urlparse(self._tlist_url), urlparse(url)
        if ((target.scheme, target.hostname, target.port) !=
                (original.scheme, original.hostname, original.port)
                or target.path != "/prod/api/v1/down"):
            self.error(f"【{self.module_name}】资源下载地址不受支持")
            return ""
        resp = self.__get_response(url)
        if resp is None:
            return ""
        try:
            torrent = bdecode(resp.content.rstrip(b"\r\n"))
            info = torrent["info"]
            if not isinstance(info, dict) or not info.get("pieces"):
                raise ValueError("缺少有效的 v1 种子信息")
            digest = hashlib.sha1(bencode(info)).hexdigest()
            trackers = []
            if torrent.get("announce"):
                trackers.append(torrent["announce"])
            for tier in torrent.get("announce-list", []):
                if isinstance(tier, list):
                    trackers.extend(tracker for tracker in tier if tracker not in trackers)
            params = []
            if info.get("name"):
                params.append(("dn", info["name"]))
            params.extend(("tr", tracker) for tracker in trackers)
            magnet = "magnet:?xt=urn:btih:" + digest
            return magnet + ("&" + urlencode(params) if params else "")
        except (BencodeDecodeError, ValueError, TypeError, KeyError, IndexError, UnicodeError) as err:
            self.error(f"【{self.module_name}】无法解析资源种子：{err}")
            return ""

    def __get_tlist_page(self, category, page):
        params = {
            "app_id": self._app_id,
            "identity": self._identity,
            "sc": category,
            "page": page,
            "limit": self._tlist_page_size,
        }
        resp = self.__get_response(self._tlist_url, params=params)
        if resp is None:
            return [], 0
        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError:
            content_type = resp.headers.get("Content-Type", "未知")
            self.error(f"【{self.module_name}】列表接口返回的不是有效 JSON（内容类型：{content_type}）")
            return [], 0
        if not data.get("success"):
            self.error(f"【{self.module_name}】列表接口返回失败：{data.get('message') or '未知错误'}")
            return [], 0
        result_data = data.get("data") or {}
        return result_data.get("list") or [], result_data.get("total") or 0