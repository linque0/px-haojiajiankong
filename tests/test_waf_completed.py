"""2026-10-05真实Chrome：完成滑块后waf_nc_block display:none，SDK样式仍在。"""
from pxb7 import collector, risk


def test_completed_waf_sdk_does_not_reject_normal_listing():
    html = '''<style>#waf_nc_block .aliyunCaptcha-show {margin-left:-24px}</style>
    <script>const cls="aliyunCaptcha-show";</script>
    <main>鸣潮账号 80级，100黄 ￥2500</main>
    <div id="waf_nc_block" style="display: none;">
      <p>访问验证 为保证您的正常访问</p>
      <div class="aliyunCaptcha-show" style="display:block">请按住滑块</div>
    </div>'''
    assert not risk.detect_dom_signals(html)
    assert "captcha" not in collector._collect_page_signals(html)


def test_active_waf_still_blocks_even_with_normal_cards():
    html = '''<main>鸣潮账号 ￥2500</main>
    <div style="display:block" id="waf_nc_block">
      <div class="aliyunCaptcha-show">请按住滑块</div>
    </div>'''
    assert risk.detect_dom_signals(html)
    assert "captcha" in collector._collect_page_signals(html)


def test_dom_component_without_challenge_text_remains_detected():
    assert risk.detect_dom_signals('<div class="aliyunCaptcha-show"><i></i></div>')


def test_hidden_ancestor_and_script_markers_are_not_active():
    html = '''<section hidden><div class="aliyunCaptcha-show"></div></section>
    <script>const x='<div id="waf_nc_block" style="display:block">';</script>'''
    assert not risk.detect_dom_signals(html)


def test_visible_text_challenge_still_blocks_without_sdk():
    assert "captcha" in collector._collect_page_signals('<main>访问验证 请按住滑块</main>')
