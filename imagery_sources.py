"""Approved imagery endpoints shared by the planner and the three downloaders."""
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

ESRI_EXPORT = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
ESRI_MAP = ESRI_EXPORT.rsplit("/", 1)[0]
EOX_CAPABILITIES = "https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml"
BING_TEMPLATE = "http://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1"
EOX_DEFAULT_LAYER = "s2cloudless-2025_3857"


def cloudless_layer(value):
    if not re.fullmatch(r"s2cloudless(?:-[0-9]{4})?_3857", value):
        raise ValueError("EOX 仅允许 Sentinel-2 cloudless 的 EPSG:3857 图层。")
    return value


def check_url(source, url):
    parts = urlsplit(url)
    if source == "esri":
        allowed = url.split("?", 1)[0] == ESRI_EXPORT
    elif source == "esri_metadata":
        endpoint = url.split("?", 1)[0]
        allowed = endpoint == ESRI_MAP or re.fullmatch(re.escape(ESRI_MAP) + r"/(?:[5-9]|1[0-8])/query", endpoint)
    elif source == "eox":
        allowed = url == EOX_CAPABILITIES or (
            parts.scheme == "https" and parts.netloc == "tiles.maps.eox.at"
            and not parts.query and re.fullmatch(
                r"/wmts/1\.0\.0/s2cloudless(?:-[0-9]{4})?_3857/[a-zA-Z0-9_-]+/"
                r"(?:GoogleMapsCompatible|g)/[0-9]+/[0-9]+/[0-9]+\.jpg", parts.path)
        )
    elif source == "bing":
        allowed = (parts.scheme in ("http", "https") and parts.netloc == "ecn.t3.tiles.virtualearth.net"
                   and re.fullmatch(r"/tiles/a[0-3]{1,23}\.jpeg", parts.path) and parts.query == "g=1")
    else:
        allowed = False
    if not allowed or parts.fragment:
        raise ValueError(f"来源限制：已阻止 {source} 清单以外的请求。")


class SourceRedirect(HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(self.source, newurl)
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.path, old.query) != (new.path, new.query):
            raise ValueError("来源限制：重定向改变了指定的图层、瓦片或查询参数。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_source(source, request, timeout):
    check_url(source, request.full_url)
    return build_opener(SourceRedirect(source)).open(request, timeout=timeout)
