"""URL 守卫 —— 真实请求前的强制校验（合规红线，docs/01 §3.2）。

规则（任一不满足即抛 UnsafeUrlError）：
1. 仅允许 http / https 两种 scheme；
2. 拒绝 URL 内的 userinfo（user:pass@host，防止把凭据写进 URL）；
3. 拒绝 localhost / 环回 / 私有 / 保留 / 链路本地 / 组播 / 未指定地址（含 IP 字面量与
   .local/.internal/.home.arpa 等内网后缀主机名）；
4. 传 allowed_hosts 时，host 必须精确命中或为白名单主机的子域；
5. resolve_dns=True 时解析主机名，任一解析结果落在私有/保留网段即拒绝
   （防 DNS rebinding / 内网探测；请求路径建议开启）。

浏览器层（pxb7.browser）另用 classify_request_url 对**每个子资源请求**做同样拦截：
- http/https/ws/wss：按 3 的地址规则判定，私有/环回/保留一律 abort；
- data/blob/about：本地内存资源，不产生外网流量，放行；
- 其余 scheme（file/ftp/chrome-extension 等）：abort。

robots.txt（docs/01 §2）：仅禁 /_nuxt/ 与 /assets/ —— 这些路径**不作为页面采集对象**
（不落 raw、不解析），但渲染所需时仍放行加载，见 is_robots_disallowed。

本模块不发起任何网络请求，只做判断；仅在第 5 条做一次 DNS 解析。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
# 子资源守卫：网络型 scheme（ws/wss 与 http/https 同等对待）
NETWORK_SCHEMES = ("http", "https", "ws", "wss")
# 本地内存型 scheme：不产生外网请求，放行
LOCAL_SCHEMES = ("data", "blob", "about")
# robots 禁抓路径（docs/01 §2：Allow: /，仅禁 /_nuxt/、/assets/）
ROBOTS_DISALLOWED_PREFIXES = ("/_nuxt/", "/assets/")

# 明确拒绝的主机名 / 后缀（不依赖 DNS）
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "0.0.0.0",
    "[::]",
}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)


class UnsafeUrlError(ValueError):
    """URL 未通过守卫校验。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """落回环/私有/保留/链路本地/组播/未指定 → 拒绝。"""
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(ip.ipv4_mapped)
    return not ip.is_global


def _host_allowed(host: str, allowed_hosts) -> bool:
    host = host.lower().rstrip(".")
    for item in allowed_hosts:
        candidate = str(item).strip().lower().rstrip(".")
        if not candidate:
            continue
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def assert_safe_url(
    url: str,
    *,
    allowed_hosts=None,
    resolve_dns: bool = False,
    require_https: bool = False,
) -> str:
    """校验并返回原 URL；不合法抛 UnsafeUrlError。

    :param allowed_hosts: 可选 host 白名单（精确匹配或子域），为空表示不限制 host。
    :param resolve_dns: 是否解析主机名并拒绝解析到内网的结果（请求路径建议 True）。
    :param require_https: True 时 http 也拒绝（本项目站点为 https）。
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL 为空")
    parts = urlsplit(url.strip())

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"仅允许 http/https，实际 scheme={parts.scheme!r}")
    if require_https and scheme != "https":
        raise UnsafeUrlError(f"要求 https，实际 scheme={scheme!r}")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL 不得包含 userinfo（禁止把凭据写进 URL）")

    host = (parts.hostname or "").strip().lower()
    if not host:
        raise UnsafeUrlError(f"URL 缺少 host：{url!r}")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError(f"拒绝本机/内网主机名：{host}")

    # IP 字面量：必须为全局可路由地址
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _is_blocked_ip(ip):
        raise UnsafeUrlError(f"拒绝私有/环回/保留 IP：{host}")

    if allowed_hosts and not _host_allowed(host, allowed_hosts):
        raise UnsafeUrlError(f"host 不在白名单内：{host}")

    if resolve_dns and ip is None:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"DNS 解析失败：{host}（{exc}）") from exc
        for info in infos:
            resolved = ipaddress.ip_address(info[4][0])
            if _is_blocked_ip(resolved):
                raise UnsafeUrlError(f"DNS 解析到内网地址：{host} -> {resolved}")

    return url.strip()


def is_safe_url(url: str, **kwargs) -> bool:
    """assert_safe_url 的布尔包装（不抛异常）。"""
    try:
        assert_safe_url(url, **kwargs)
    except UnsafeUrlError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 浏览器子资源守卫（Playwright route 拦截 / 导航前校验）
# --------------------------------------------------------------------------- #
def _split(url: str):
    try:
        return urlsplit(url.strip())
    except ValueError:
        return None


def classify_request_url(url: str) -> tuple[bool, str]:
    """子资源请求放行判定：返回 (allowed, reason)。

    与 assert_safe_url 的差别：不做 host 白名单（页面可能加载 CDN 静态资源），
    但同样拒绝私有/环回/保留地址与非网络 scheme。被拒的 URL 绝不会被请求。
    """
    if not isinstance(url, str) or not url.strip():
        return (False, "empty-url")
    parts = _split(url)
    if parts is None:
        return (False, "unparsable-url")
    scheme = (parts.scheme or "").lower()
    if scheme in LOCAL_SCHEMES:
        return (True, "local-scheme")
    if scheme not in NETWORK_SCHEMES:
        return (False, f"scheme-not-allowed:{scheme or 'none'}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        return (False, "missing-host")
    if parts.username or parts.password:
        return (False, "userinfo-present")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return (False, f"blocked-hostname:{host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _is_blocked_ip(ip):
        return (False, f"blocked-ip:{host}")
    return (True, "ok")


def is_robots_disallowed(url: str) -> bool:
    """是否命中 robots 禁抓路径（/_nuxt/、/assets/）。

    命中者不作为**页面采集对象**：不落 raw、不解析；渲染需要时仍可加载。
    """
    parts = _split(url)
    if parts is None:
        return False
    path = parts.path or ""
    return any(path.startswith(prefix) for prefix in ROBOTS_DISALLOWED_PREFIXES)


def assert_page_url(url: str, *, allowed_hosts=None, require_https: bool = True,
                    resolve_dns: bool = False) -> str:
    """页面导航专用守卫：http/https + host 白名单 + 私有地址拒绝（可选 DNS 复核）。

    失败即抛错——调用方必须在此之前不发起任何请求。
    """
    return assert_safe_url(url, allowed_hosts=allowed_hosts, require_https=require_https,
                           resolve_dns=resolve_dns)
