import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from app.indexer.indexerConf import IndexerConf
from app.plugins.modules._base import _IPluginModule
from app.utils import RequestUtils, ExceptionUtils, StringUtils
from config import Config


class Bitba(_IPluginModule):
    # 插件名称
    module_name = "BiT吧"
    module_desc = "让内建索引器支持检索bitba.net站点资源（磁力链接直接获取，无需下载种子）"
    module_icon = "jackett.png"
    module_color = "#35D07F"
    module_version = "0.1"
    module_author = "leeson"
    author_url = ""
    module_config_prefix = "bitba_"
    module_order = 22
    auth_level = 1

    # 私有属性
    _enable = False
    _domain = "https://www.bitba.net"
    # 站内搜索建议接口，直接返回剧目 did 列表，不支持分页
    _search_api = "https://search.bitba.xiaoeryi.com/index"
    # 磁力信息获取接口，按 did+hash 换取真实磁力链接
    _magnet_api = "https://torrent.baidu.com.btba.xiaoeryi.com/download_quick"
    # 详情页/磁力接口的并发抓取数，调太高容易被站点风控识别为异常流量
    _detail_concurrency = 3
    # 每次搜索最多处理的剧目（did）数量，避免关键字命中过多剧目时请求量爆炸
    _max_dids = 10
    _ua = None
    # 详情页/磁力接口请求复用同一个连接池（keep-alive），省掉重复握手开销
    _session = None

    @staticmethod
    def get_fields():
        return [
            {
                'type': 'div',
                'content': [
                    [
                        {
                            'title': '站点地址',
                            'required': "required",
                            'type': 'text',
                            'content': [
                                {'id': 'domain', 'placeholder': 'https://www.bitba.net'}
                            ]
                        }
                    ],
                    [
                        {
                            'title': '每次搜索最多剧目数',
                            'required': "",
                            'tooltip': '关键字命中的剧目（如某剧不同版本/分集资源）数量上限，越大耗时越长，默认10',
                            'type': 'text',
                            'content': [
                                {'id': 'max_dids', 'placeholder': '10'}
                            ]
                        }
                    ],
                    [
                        {
                            'title': '详情页并发数',
                            'required': "",
                            'tooltip': '同时抓取详情页/磁力接口的线程数，调太高容易被站点风控识别为异常流量，默认3',
                            'type': 'text',
                            'content': [
                                {'id': 'detail_concurrency', 'placeholder': '3'}
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
            self._domain = str(config.get("domain") or self._domain).rstrip("/")
            try:
                self._max_dids = max(1, int(config.get("max_dids") or self._max_dids))
            except (TypeError, ValueError):
                pass
            try:
                self._detail_concurrency = max(1, int(config.get("detail_concurrency") or self._detail_concurrency))
            except (TypeError, ValueError):
                pass
            self._enable = config.get("enable")
        self._ua = Config().get_ua()
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self._detail_concurrency, pool_maxsize=self._detail_concurrency
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

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
                "id": "bitba-plugin",
                "name": "BiT吧(插件)",
                "domain": self._domain,
                "public": True,
                "builtin": False,
                "proxy": False,
                "parser": self.__class__.__name__,
            })
        ]

    def _headers(self):
        return {"User-Agent": self._ua}

    @staticmethod
    def _proxies():
        return Config().get_proxies()

    def search(self, indexer, keyword, page=0, filter_args=None):
        if not indexer or not keyword:
            return []

        dids = self.__search_dids(keyword)
        if not dids:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
            return []

        # 每个 did 详情页里包含该剧目的全部资源（磁力hash），先并发抓详情页拿到资源清单
        torrents = []
        with ThreadPoolExecutor(max_workers=min(self._detail_concurrency, len(dids))) as executor:
            futures = [executor.submit(self.__parse_detail_page, did) for did in dids]
            for future in as_completed(futures):
                try:
                    torrents.extend(future.result())
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)

        if not torrents:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
            return []

        # 资源清单里没有磁力链接，需要逐条用 did+hash 换取真实磁力
        results = []
        with ThreadPoolExecutor(max_workers=min(self._detail_concurrency, len(torrents))) as executor:
            future_to_item = {}
            for item in torrents:
                future_to_item[executor.submit(self.__get_magnet, item["did"], item["hash"])] = item
                # 错开提交时间，避免瞬时并发全部命中同一时刻
                time.sleep(0.1)
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    magnet = future.result()
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    continue
                if not magnet:
                    continue
                results.append({
                    "indexer_id": indexer.id,
                    "indexer": indexer.name,
                    "title": item["title"],
                    "enclosure": magnet,
                    "description": "",
                    "size": item["size"],
                    # 站点没有做种数，用"热度"代替展示，仅供参考
                    "seeders": item["heat"],
                    "peers": 0,
                    "freeleech": False,
                    "downloadvolumefactor": 1.0,
                    "uploadvolumefactor": 1.0,
                    "page_url": item["page_url"],
                    "imdbid": "",
                })

        if results:
            self.warn(f"【{self.module_name}】{indexer.name} 共查询 {len(dids)} 部剧目，返回磁力数据：{len(results)}")
        else:
            self.warn(f"【{self.module_name}】{indexer.name} 未搜索到数据")
        return results

    def __search_dids(self, keyword):
        """
        站内搜索建议接口，返回匹配的剧目 did 列表（不含具体资源/磁力信息）
        """
        try:
            resp = RequestUtils(
                headers={"User-Agent": self._ua, "Content-Type": "application/json"},
                proxies=self._proxies(),
                timeout=10,
            ).post_res(url=self._search_api, json={"q": keyword, "limit": self._max_dids})
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return []
        if not resp or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return []
        hits = data.get("hits") or []
        return [hit.get("did") for hit in hits if hit.get("did")][:self._max_dids]

    def __parse_detail_page(self, did):
        """
        剧目详情页 -> 资源清单（标题/大小/热度/磁力hash），本身不含磁力链接
        """
        page_url = f"{self._domain}/bt/{did}.html"
        try:
            resp = RequestUtils(
                headers=self._headers(), proxies=self._proxies(), session=self._session, timeout=10
            ).get_res(url=page_url)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return []
        if not resp or resp.status_code != 200:
            return []
        resp.encoding = resp.apparent_encoding or "utf-8"

        torrents = []
        for match in re.finditer(
            r'<a href="[^"]*?/down/(\d+)/([0-9a-f]+)\.html"[^>]*title="([^"]+)"[^>]*>.*?</a>\s*'
            r'<b>([\d.]+\s*[TGMK]?B)</b>.*?'
            r'热度:(\d+)',
            resp.text,
            re.DOTALL,
        ):
            item_did, torrent_hash, title, size_text, heat = match.groups()
            torrents.append({
                "did": item_did,
                "hash": torrent_hash,
                "title": html.unescape(title),
                "size": StringUtils.num_filesize(size_text),
                "heat": int(heat),
                "page_url": page_url,
            })
        return torrents

    def __get_magnet(self, did, torrent_hash):
        try:
            resp = RequestUtils(
                headers=self._headers(), proxies=self._proxies(), session=self._session, timeout=10
            ).post_res(url=self._magnet_api, data={"did": did, "hash": torrent_hash})
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return ""
        if not resp or resp.status_code != 200:
            return ""
        try:
            data = resp.json()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return ""
        return data.get("magnet") or ""
