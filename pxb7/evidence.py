"""保留可重放的公开文字DOM，剔除脚本、图片内容和表单/认证属性。"""
from bs4 import BeautifulSoup


def compact(html, *, page_type):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,iframe,object,embed,canvas,svg,video,audio,source,link,meta,input,textarea,select,.seller-info,.seller-contact"):
        node.decompose()
    for node in soup.select("img"):
        alt = node.get("alt")
        if alt:
            replacement = soup.new_tag("span")
            replacement.string = alt
            node.replace_with(replacement)
        else:
            node.decompose()
    for node in soup.find_all(True):
        style = str(node.get("style", ""))
        for key in list(node.attrs):
            if key.lower().startswith("on") or any(word in key.lower() for word in ("token", "authorization", "cookie", "captcha", "password")) \
                    or key.lower() in ("style", "src", "srcset", "value", "action"):
                del node.attrs[key]
            elif key == "href" and not str(node.attrs[key]).startswith(("/buy/", "/product/", "https://www.pxb7.com/buy/", "https://www.pxb7.com/product/")):
                del node.attrs[key]
        # 保留解析器识别隐藏模板的语义，不保留任意内联样式/URL。
        if "display:none" in style.replace(" ", "").lower():
            node["style"] = "display:none"
        elif "visibility:hidden" in style.replace(" ", "").lower():
            node["style"] = "visibility:hidden"
    return str(soup)
