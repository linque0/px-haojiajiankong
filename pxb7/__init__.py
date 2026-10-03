"""pxb7-price-monitor：螃蟹游戏服务网（pxb7）账号挂牌价格采集与监测。

骨架阶段（W1）包含：配置加载/校验、DuckDB 数据层、CLI 框架。
红线（docs/01 §3.2）：不做协议逆向、不直连加密 API、不做指纹伪造与代理池；
所有真实请求必须经 pxb7.urlguard.assert_safe_url 校验。
"""

__version__ = "0.1.0"
