"""Versioned public detailPost facts, without manufacturing an HTML document.

Only normal browser-observed responses enter this adapter. Unsupported reports,
missing sections and conflicting rosters require the existing DOM fallback.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from . import extract as X, parser as P, exploration as E, weapons as W


def integer(value, maximum=100000000):
    if isinstance(value, bool) or value is None:
        return None
    if not re.fullmatch(r"\d+", str(value)):
        return None
    number = int(value)
    return number if number <= maximum else None


def validate_product(d):
    def obj(value, keys):
        if not isinstance(value, dict) or set(value)-set(keys):
            raise ValueError("invalid-public-detail-shape")
    def rows(value):
        if not isinstance(value, list) or len(value)>2000:
            raise ValueError("invalid-public-detail-shape")
        return value
    def leaf(value, keys):
        obj(value,keys)
        if any(v is not None and (not isinstance(v,(str,int,float,bool)) or isinstance(v,str) and len(v)>50000)
               for v in value.values()):
            raise ValueError("invalid-public-detail-shape")
    obj(d,("id","game_id","price_fen","status","title","attrs","report_attrs","report","favorites",
           "published_at","published_text","remark","guarantee"))
    for a in rows(d.get("attrs",[])):
        obj(a,("attrName","aliasName","values"))
        for v in rows(a.get("values",[])):
            leaf(v,("itemName","attrValue"))
    for a in rows(d.get("report_attrs",[])):
        leaf(a,("attrName","attrValue"))
    report=d.get("report")
    if report is None:
        return
    obj(report,("tabs","groups"))
    for t in rows(report.get("tabs",[])):
        leaf(t,("categoryId","categoryName"))
    for g in rows(report.get("groups",[])):
        obj(g,("groupName","tabId","groupEleSize","elements"))
        for e in rows(g.get("elements",[])):
            obj(e,("role","weapon","map","item"))
            for key,keys in {"role":("roleName","roleLevel","roleRarity","mingZuo","specializedWeapon","equipped_weapon"),
                             "weapon":("weaponName","weaponLevel","weaponRarity","weaponRefineNum"),
                             "map":("mapName","explorePlan"),"item":("name","count","level","assetsNum")}.items():
                if key=="role":
                    role=e.get(key,{})
                    obj(role,keys)
                    leaf(role.get("equipped_weapon",{}),("weaponName","weaponLevel","weaponRarity","weaponRefineNum"))
                    leaf({k:v for k,v in role.items() if k!="equipped_weapon"},tuple(k for k in keys if k!="equipped_weapon"))
                else:leaf(e.get(key,{}),keys)


def parse(payload):
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1 or payload.get("source") != "detailPost":
        raise ValueError("unsupported-detail-schema")
    d = payload.get("product")
    if not isinstance(d, dict) or d.get("id") != payload.get("listing_id") or not re.fullmatch(
            r"https://www\.pxb7\.com/product/" + re.escape(str(d.get("id"))) + r"/1", payload.get("url", "")):
        raise ValueError("detail-id-mismatch")
    if str(d.get("game_id")) != "10302":
        raise ValueError("unsupported-detail-game")
    validate_product(d)
    if type(d.get("status")) is not int or d.get("status") != 1:
        raise ValueError("delisted" if d.get("status") == 2 else "unsupported-detail-status")
    try:
        if not isinstance(payload.get("observed_at"),str):
            raise ValueError()
        observed = dt.datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        if observed.tzinfo is None or not -300 <= (now-observed).total_seconds() <= 600:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid-detail-observation-time") from None
    title = d.get("title")
    if not isinstance(title, str) or not 6 <= len(title) <= 50000 or not re.search(
            r"详情看图|官方截图", title) or re.search(r"\.\.\.|…|⋯|访问验证|请按住滑块", title):
        raise ValueError("incomplete-detail-response")
    try:
        price = Decimal(str(d["price_fen"]))
        if not price.is_finite() or price <= 0 or price != price.to_integral():
            raise ValueError()
    except (KeyError, InvalidOperation, ValueError):
        raise ValueError("invalid-detail-price") from None
    attrs, pieces, conflicts = {}, [], []
    names = {"等级":"level", "联觉等级":"level", "黄数":"yellow_cnt", "五星角色数量":"five_star_chars",
             "五星武器数量":"five_star_weapons", "五星武器":"five_star_weapons"}
    facts=[]
    for a in d.get("attrs", []):
        if not isinstance(a, dict):
            raise ValueError("invalid-detail-attributes")
        vals=[v.get("itemName") if v.get("itemName") is not None else v.get("attrValue") for v in a.get("values", [])]
        facts.extend((a.get("attrName"), v) for v in vals if v is not None)
    facts.extend((a.get("attrName"),a.get("attrValue")) for a in d.get("report_attrs", []) if a.get("attrValue") is not None)
    for name, value in facts:
        direct={"TAP绑定情况":"tap_status","是否有换绑CD":"trade_code_status"}.get(name)
        if direct and isinstance(value,str):
            if direct in attrs and attrs[direct]!=value: conflicts.append(direct)
            attrs[direct]=value
        pieces.append(f"{name}：{value}")
        key=names.get(name)
        number=integer(value)
        if key and number is not None:
            if key in attrs and attrs[key] != number:
                conflicts.append(key)
            attrs[key]=number
    text="；".join([title, *pieces, d.get("remark") or ""])
    # Share the original text conversion rules, never create synthetic markup.
    for key in ("level","yellow_cnt","five_star_chars","five_star_weapons","server","mail_status","tap_status",
                "psn_status","trade_code_status","has_compensation","official_verified","skins"):
        hit=P._extract(None,key,tuple(s for s in P.FIELD_SPECS[key] if s.kind=="regex"),title if key=="level" else text)
        if hit:
            if key in attrs and key in names.values() and attrs[key] != hit.value:
                conflicts.append(key)
            attrs.setdefault(key,hit.value)
    if attrs.get("five_star_weapons") is None and W.explicit_empty(text):
        attrs["five_star_weapons"]=0
    if d.get("published_at"):
        attrs["publish_time_text"]=str(d["published_at"])
    ex=X.extract_listing(X.seed_keywords(game_id=10302),listing_id=d["id"],title=title,card_fields=attrs)
    for key,label in X.WUWA_RESOURCES:
        vals={integer(value) for name,value in facts if name==label and integer(value) is not None}
        if len(vals)>1 or vals and ex.features.get(key) not in (None,next(iter(vals))):
            conflicts.append(key)
        if vals:
            ex.features[key]=next(iter(vals))
    report=d.get("report")
    if not isinstance(report,dict) or not report.get("tabs") or not isinstance(report.get("groups"),list):
        raise ValueError("detail-report-not-ready")
    groups=report["groups"]
    advertised={str(t.get("categoryName")) for t in report["tabs"]}
    maps=[]
    for category in advertised:
        needles={"角色":"角色", "武器":"武器", "地图":"地图", "地图探索度":"地图", "服饰":"服饰"}
        if category not in needles or category != "服饰" and not any(needles[category] in str(g.get("groupName")) for g in groups):
            raise ValueError("detail-report-not-ready")
    for label,key,sub,name_key,value_key,upper in (
            ("五星角色","five_star_character_chains","role","roleName","mingZuo",6),
            ("五星武器","five_star_weapon_refinements","weapon","weaponName","weaponRefineNum",5)):
        matching=[g for g in groups if g.get("groupName")==label]
        if not matching and attrs.get("five_star_chars" if sub=="role" else "five_star_weapons") != 0:
            raise ValueError("detail-report-not-ready")
        roster=[]
        for g in matching:
            for e in g.get("elements",[]):
                item=e.get(sub) or {}
                value=integer(item.get(value_key),upper)
                if not item.get(name_key) or value is None or sub=="weapon" and value==0:
                    raise ValueError("detail-report-not-ready")
                roster.append({"name":item[name_key],"value":value})
        expected=attrs.get("five_star_chars" if sub=="role" else "five_star_weapons")
        if expected is None or expected != len(roster):
            conflicts.append(key)
        from collections import Counter
        # API skin prefix conventions do not affect core roster comparison.
        if ex.features.get(key) and Counter((x["name"],x["value"]) for x in ex.features[key]) != Counter((x["name"],x["value"]) for x in roster):
            conflicts.append(key)
        ex.features[key]=roster
    for g in groups:
        if "地图" in str(g.get("groupName")):
            elements=g.get("elements",[])
            if integer(g.get("groupEleSize")) != len(elements):
                raise ValueError("detail-map-not-ready")
            for e in elements:
                m=e.get("map") or {}
                if not m.get("mapName") or E.percent(m.get("explorePlan")) is None:
                    raise ValueError("detail-map-not-ready")
                maps.append({"name":m["mapName"],"progress":E.percent(m["explorePlan"])})
    if conflicts:
        raise ValueError("detail-field-conflict:"+",".join(sorted(set(conflicts))))
    ex.features[E.FEATURE]=E.normalize(maps)
    ex.features["_detail_response_evidence"]={"source":"detailPost","schema_version":1,
        "attrs":d.get("attrs",[]),"report_attrs":d.get("report_attrs",[]),"report":report,
        "remark":d.get("remark"),"guarantee":d.get("guarantee"),"observed_at":observed.isoformat(),
        "asset_report_time":None}  # Current response observation is not the historical verification date.
    ex.features[W.FEATURE]=W.observe(title,text,attrs,ex.features)
    return {"title":title,"attributes":attrs,"features":ex.features,"text":text,
            "observed_at":observed.isoformat(),"price_yuan":float(price/100),
            "fields":{"favorites_cnt":integer(d.get("favorites")),"viewers_masked":None,
                      "viewers_visible":False,"viewers_mask_detected":False}}
