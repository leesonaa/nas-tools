import json
import re
from html import unescape
from urllib.parse import urlparse

import requests

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
    module_version = "0.2"
    module_author = "leeson"
    author_url = ""
    module_config_prefix = "btl_"
    module_order = 21
    auth_level = 1

    _enable = False
    _domain = "https://web5.mukaku.com"
    # getVideoDetail 已被站点加了 VIP 限制（need_vip），拿不到磁力；
    # getTList（首页"最新资源列表"接口）不受此限制，且列表数据里直接带 zlink，改用这个
    _tlist_url = "https://web5.mukaku.com/prod/api/v1/getTList"
    _app_id = "83768d9ad4"
    _identity = "23734adac0301bccdcb107c4aa21f96c"
    # 站点分类：1=电影 2=电视剧，getTList 不支持关键字搜索，只能挨个分类翻页后本地匹配标题
    _tlist_categories = (1, 2)
    # 站点固定每页返回 20 条，total 固定 400，即每个分类最多 20 页
    _tlist_page_size = 20
    # 连接池大小，翻页请求按顺序执行以便在找到首集后及时停止
    _tlist_concurrency = 5
    # 接口允许超出 total 继续翻页，设置上限避免站点异常时无限请求
    _tlist_max_pages = 500
    _ua = None
    # 复用同一个连接池（keep-alive），省掉重复握手开销
    _session = None

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
        self._ua = Config().get_ua()
        self._session = requests.Session()
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

    def search(self, indexer, keyword, page=0):
        if not indexer or not keyword:
            return []

        entries = self.__collect_tlist_entries(keyword)
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
    def __is_first_episode(entry, keyword):
        if not Btl.__entry_matches_keyword(entry, keyword):
            return False
        title = " ".join(str(entry.get(field) or "") for field in ("title", "zname"))
        return bool(
            re.search(r"(?<![A-Z0-9])S0*1[\s._-]*E0*1(?![A-Z0-9])", title, re.IGNORECASE)
            or re.search(r"(?<![A-Z0-9])1\s*[xX]\s*0*1(?![A-Z0-9])", title)
            or re.search(r"第\s*0*1\s*[集话話]", title)
        )

    def __collect_tlist_entries(self, keyword):
        """
        getTList 不支持关键字搜索，只能把"电影"、"电视剧"两个分类的全部分页拉完，
        再本地按标题匹配关键字。站点虽然返回 total，但超过 total 仍可能有数据，
        因此按页递增；找到当前剧集的 S01E01 后即可停止。
        """
        entries = []
        finished_categories = set()
        page_signatures = {category: set() for category in self._tlist_categories}

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
                entries.extend(page_list)

                if any(self.__is_first_episode(entry, keyword) for entry in page_list):
                    return entries

            if len(finished_categories) == len(self._tlist_categories):
                break

        return entries

    def __get_tlist_page(self, category, page):
        params = {
            "app_id": self._app_id,
            "identity": self._identity,
            "sc": category,
            "page": page,
            "limit": self._tlist_page_size,
        }
        try:
            resp = RequestUtils(
                headers=self._headers(), proxies=self._proxies(), session=self._session, timeout=15
            ).get_res(url=self._tlist_url, params=params)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return [], 0
        if not resp or resp.status_code != 200:
            return [], 0
        try:
            data = resp.json()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return [], 0
        if not data.get("success"):
            return [], 0
        result_data = data.get("data") or {}
        return result_data.get("list") or [], result_data.get("total") or 0
