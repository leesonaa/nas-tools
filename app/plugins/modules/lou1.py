import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.indexer.indexerConf import IndexerConf
from app.plugins.modules._base import _IPluginModule
from app.sites import Sites
from app.utils import RequestUtils, ExceptionUtils, StringUtils
from config import Config


class Lou1(_IPluginModule):
    # 插件名称
    module_name = "1lou"
    module_desc = "让内建索引器支持检索1lou.me站点资源（支持多镜像域名切换，下载自动走代理）"
    module_icon = "lou1.png"
    module_color = "#2D2D2D"
    module_version = "0.4"
    module_author = "leeson"
    author_url = ""
    module_config_prefix = "lou1_"
    module_order = 20
    auth_level = 1

    # 私有属性
    _enable = False
    # 镜像域名列表，按顺序尝试，第一个能连通的就用哪个（同时会被逐个注册进"站点管理"并开启代理）
    _mirror_domains = ["https://www.1lou.me", "https://1lou.vip"]
    # 搜索接口固定用这个（目前只确认 www.1lou.me 有这个 JSON 接口）
    _search_api = "https://www.1lou.me/search/api/search.php"
    _ua = None

    @staticmethod
    def get_fields():
        return [
            {
                'type': 'div',
                'content': [
                    [
                        {
                            'title': '镜像域名列表',
                            'required': "required",
                            'tooltip': '逗号分隔，按顺序尝试，前面的连不通自动换下一个。'
                                       '这些域名会自动注册进"站点管理"并开启代理，下载种子时会自动带上代理请求',
                            'type': 'text',
                            'content': [
                                {'id': 'mirror_domains', 'placeholder': 'https://www.1lou.me,https://1lou.vip'}
                            ]
                        }
                    ],
                    [
                        {
                            'title': '搜索接口地址',
                            'required': "required",
                            'tooltip': '一般不用改，除非1lou.me自己换了域名/路径',
                            'type': 'text',
                            'content': [
                                {'id': 'search_api', 'placeholder': 'https://www.1lou.me/search/api/search.php'}
                            ]
                        }
                    ],
                    [
                        {
                            'title': '启用',
                            'required': "",
                            'type': 'switch',
                            'id': 'enable'
                        }
                    ]
                ]
            }
        ]

    def init_config(self, config=None):
        if config:
            domains_str = config.get("mirror_domains") or ",".join(self._mirror_domains)
            self._mirror_domains = [
                d.strip().rstrip("/") for d in domains_str.split(",") if d.strip()
            ]
            self._search_api = config.get("search_api") or self._search_api
            self._enable = config.get("enable")
        self._ua = Config().get_ua()
        if self._enable:
            self.__register_sites_with_proxy()

    def get_state(self):
        return self._enable

    def stop_service(self):
        pass

    def get_indexers(self):
        """
        声明这个插件提供的"站点"，parser 必须等于插件类名（nas-tools 内部按类名注册插件）
        """
        if not self._enable:
            return []
        return [
            IndexerConf({
                "id": "1lou-plugin",
                "name": "1lou(插件)",
                "domain": self._mirror_domains[0] if self._mirror_domains else "",
                "public": True,
                "builtin": False,
                "proxy": True,
                "parser": self.__class__.__name__,
            })
        ]

    def __register_sites_with_proxy(self):
        """
        把每个镜像域名注册进"站点管理"并开启代理开关（note里写 {"proxy":"Y"}）。
        这样后续 nas-tools 下载种子时会自动按这个域名的配置带上代理请求，
        不用再手动去站点管理页面配置，也不用靠切换域名绕开代理问题。
        已经注册过的域名跳过，不重复添加。
        """
        try:
            sites = Sites()
            for domain in self._mirror_domains:
                existing = sites.get_sites_by_url_domain(domain)
                if existing:
                    continue
                host = urlparse(domain).netloc or domain
                sites.add_site(
                    name=f"1lou-{host}",
                    site_pri=0,
                    rssurl=domain + "/",
                    note=json.dumps({"proxy": "Y"}),
                )
                self.info(f"【{self.module_name}】已将 {domain} 注册为站点并开启代理")
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.error(f"【{self.module_name}】自动注册站点代理失败: {e}")

    def _headers(self):
        return {"User-Agent": self._ua, "Accept": "application/json"}

    @staticmethod
    def __normalize_title(title):
        title = re.sub(r"^\[(?:BT下载|BT|磁力下载|种子下载)\]\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(r"([.\s()[\]_-])(?:19\d{2}|20[0-4]\d)(?=$|[.\s()[\]_-])", r"\1", title)
        return re.sub(r"\.{2,}", ".", title)

    @staticmethod
    def _proxies():
        # 插件自己发的请求（搜索接口、详情页解析）统一走 nas-tools 全局代理设置
        return Config().get_proxies()

    def search(self, indexer, keyword, page=0):
        if not indexer or not keyword:
            return []

        page_no = 1
        total = 0
        page_size = 50
        hits = []
        seen_threads = set()
        while True:
            page_hits, page_total, page_size = self.__search_api(keyword, page=page_no)
            if not page_hits:
                break
            hits.extend(page_hits)
            total = page_total or total
            if total and page_no * page_size >= total:
                break
            page_no += 1

        if not hits:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
            return []

        results = []
        for hit in hits:
            # 没有附件的帖子（比如简介占位帖）直接跳过，不用浪费一次详情页请求
            if not hit.get("files"):
                continue
            thread_path = hit.get("thread_url", "")
            if not thread_path or thread_path in seen_threads:
                continue
            seen_threads.add(thread_path)
            try:
                enclosure, size, page_url = self.__parse_detail(thread_path)
                if not enclosure:
                    continue
                results.append({
                    "indexer_id": indexer.id,
                    "indexer": indexer.name,
                    "title": self.__normalize_title(hit.get("subject", "")),
                    "enclosure": enclosure,
                    "description": "",
                    "size": size or 0,
                    "seeders": 0,
                    "peers": 0,
                    "freeleech": False,
                    "downloadvolumefactor": 1.0,
                    "uploadvolumefactor": 1.0,
                    "page_url": page_url,
                    "imdbid": "",
                })
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                continue

        if results:
            self.warn(f"【{self.module_name}】{indexer.name} 共查询 {total or len(hits)} 条，返回附件数据：{len(results)}")
        else:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
        return results

    def __search_api(self, keyword, page=1):
        """
        按主域名优先调用搜索接口，主域名失效后再尝试镜像。
        """
        params = {
            "q": keyword,
            "page": page,
            "sort": "newest",
            "scope": "全部",
            "type": "全部",
            "year": "全部",
            "quality": "全部",
            "source": "全部",
            "track": 0,
        }
        api_path = urlparse(self._search_api).path.lstrip("/")
        for domain in self._mirror_domains:
            api_url = urljoin(domain + "/", api_path)
            try:
                resp = RequestUtils(
                    headers=self._headers(), proxies=self._proxies(), timeout=15
                ).get_res(url=api_url, params=params)
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                continue
            if not resp or resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                continue
            if not data.get("ok"):
                continue
            result_data = data.get("data") or {}
            return (
                result_data.get("hits") or [],
                result_data.get("total") or 0,
                result_data.get("page_size") or 50,
            )
        return [], 0, 50

    def __parse_detail(self, thread_path):
        """
        详情页 -> 附件下载链接。依次尝试镜像域名列表，哪个能连通就用哪个。
        1lou 的附件链接固定格式：/attach-download-<id>.htm，文件名里通常带了体积文本
        """
        last_error = None
        for domain in self._mirror_domains:
            page_url = urljoin(domain + "/", thread_path.lstrip("/"))
            try:
                resp = RequestUtils(
                    headers=self._headers(), proxies=self._proxies(), timeout=10
                ).get_res(url=page_url)
            except Exception as e:
                last_error = e
                continue
            if not resp or resp.status_code != 200:
                continue

            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
            attach_tag = soup.find("a", href=re.compile(r"attach-download-\d+\.htm"))
            if not attach_tag:
                # 主域名内容异常时继续尝试备用镜像。
                continue

            enclosure = urljoin(domain + "/", attach_tag.get("href"))
            filename = attach_tag.get_text(strip=True)

            size = 0
            size_match = re.search(r"([\d.]+\s*[TGMK]B)", filename, re.IGNORECASE)
            if size_match:
                size = StringUtils.num_filesize(size_match.group(1))

            return enclosure, size, page_url

        if last_error:
            self.error(f"【{self.module_name}】所有镜像域名均无法访问: {last_error}")
        return "", 0, ""