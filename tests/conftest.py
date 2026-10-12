"""离线回归的站点 DNS 固定夹具；不查询外部 DNS，不改变生产 URL 校验。"""
import socket
import pytest


@pytest.fixture(autouse=True)
def offline_site_dns(monkeypatch):
    original = socket.getaddrinfo
    def lookup(host, port, *args, **kwargs):
        if str(host).lower() in {'www.pxb7.com', 'pxb7.com', 'open.feishu.cn'}:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', int(port)))]
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
