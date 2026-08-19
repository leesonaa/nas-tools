import json
import re
from html import unescape
from urllib.parse import urlparse

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
    module_version = "0.1"
    module_author = "leeson"
    author_url = ""
    module_config_prefix = "btl_"
    module_order = 21
    auth_level = 1

    _enable = False
    _domain = "https://web5.mukaku.com"
    _api_url = "https://web5.mukaku.com/prod/api/v1/getVideoList"
    _detail_url = "https://web5.mukaku.com/prod/api/v1/getVideoDetail"
    _app_id = "83768d9ad4"
    _identity = "23734adac0301bccdcb107c4aa21f96c"
    _ua = None

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
                            "title": "API地址",
                            "required": "required",
                            "tooltip": "一般不用修改，站点更换域名时需要同步修改",
                            "type": "text",
                            "content": [
                                {"id": "api_url", "placeholder": "https://web5.mukaku.com/prod/api/v1/getVideoList"}
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
            self._api_url = config.get("api_url") or self._api_url
            self._enable = config.get("enable")
        self._ua = Config().get_ua()
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

    def search(self, indexer, keyword, page=0):
        if not indexer or not keyword:
            return []

        page_no = 1
        page_size = 35
        total = 0
        results = []
        seen_links = set()
        while True:
            entries, page_total, page_limit = self.__search_api(keyword, page_no, page_size)
            if not entries:
                break
            total = page_total or total
            page_size = page_limit or page_size
            for entry in entries:
                if not self.__entry_matches_keyword(entry, keyword):
                    self.warn(
                        f"【{self.module_name}】跳过非同名影视条目：{entry.get('title', '')}"
                    )
                    continue
                detail = self.__get_detail(entry.get("doub_id") or entry.get("idcode"))
                if not self.__detail_matches_entry(entry, detail):
                    self.warn(
                        f"【{self.module_name}】跳过标题不一致的详情："
                        f"{entry.get('title', '')} -> {detail.get('title', '')}"
                    )
                    continue
                for torrent in self.__extract_torrents(detail):
                    enclosure = torrent.get("zlink") or torrent.get("magnet") or ""
                    if not enclosure.lower().startswith("magnet:") or enclosure in seen_links:
                        continue
                    seen_links.add(enclosure)
                    title = torrent.get("zname") or entry.get("title") or ""
                    if not title:
                        continue
                    results.append(
                        {
                            "indexer_id": indexer.id,
                            "indexer": indexer.name,
                            "title": self.__normalize_title(title),
                            "enclosure": enclosure,
                            "description": torrent.get("conta") or entry.get("abstract") or "",
                            "size": self.__parse_size(torrent.get("zsize") or torrent.get("ezsize")),
                            "seeders": 0,
                            "peers": 0,
                            "freeleech": False,
                            "downloadvolumefactor": 1.0,
                            "uploadvolumefactor": 1.0,
                            "page_url": self._domain,
                            "imdbid": entry.get("IMDB_number") or "",
                        }
                    )
            if total and page_no * page_size >= total:
                break
            if len(entries) < page_size:
                break
            page_no += 1

        if results:
            self.warn(f"【{self.module_name}】{indexer.name} 共查询 {total or len(results)} 条，返回磁力数据：{len(results)}")
        else:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
        return results

    @staticmethod
    def __detail_matches_entry(entry, detail):
        entry_title = Btl.__normalize_match_title(entry.get("title"))
        detail_title = Btl.__normalize_match_title(detail.get("title"))
        if not entry_title or not detail_title:
            return False
        return entry_title in detail_title or detail_title in entry_title

    @staticmethod
    def __normalize_match_title(title):
        title = unescape(str(title or "")).casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", title)

    @staticmethod
    def __entry_matches_keyword(entry, keyword):
        keyword = Btl.__normalize_match_title(keyword)
        if not keyword:
            return False
        candidates = [entry.get("title"), entry.get("otitle")]
        alias = entry.get("alias") or ""
        candidates.extend(re.split(r"[,，/|、]", str(alias)))
        return any(Btl.__normalize_match_title(candidate) == keyword for candidate in candidates)

    def __get_detail(self, video_id):
        if not video_id:
            return {}
        params = {
            "app_id": self._app_id,
            "identity": self._identity,
            "id": video_id,
        }
        try:
            resp = RequestUtils(
                headers=self._headers(), proxies=self._proxies(), timeout=20
            ).get_res(url=self._detail_url, params=params)
            if not resp or resp.status_code != 200:
                return {}
            data = resp.json()
            return data.get("data") or {} if data.get("success") else {}
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return {}

    @staticmethod
    def __extract_torrents(detail):
        torrents = detail.get("all_seeds") or []
        if isinstance(torrents, list) and torrents:
            return torrents
        grouped_torrents = detail.get("ecca") or {}
        if isinstance(grouped_torrents, dict):
            torrents = []
            for group in grouped_torrents.values():
                if isinstance(group, list):
                    torrents.extend(group)
            if torrents:
                return torrents
        if detail.get("zlink"):
            return [detail]
        return []

    def __search_api(self, keyword, page, page_size):
        params = {
            "app_id": self._app_id,
            "identity": self._identity,
            "sb": keyword,
            "page": page,
            "limit": page_size,
        }
        try:
            resp = RequestUtils(
                headers=self._headers(), proxies=self._proxies(), timeout=20
            ).get_res(url=self._api_url, params=params)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return [], 0, page_size
        if not resp or resp.status_code != 200:
            return [], 0, page_size
        try:
            data = resp.json()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return [], 0, page_size
        if not data.get("success"):
            return [], 0, page_size
        result_data = data.get("data") or {}
        return (
            result_data.get("data") or [],
            result_data.get("total") or 0,
            result_data.get("limit") or page_size,
        )
