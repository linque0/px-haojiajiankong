/** Passive normal detailPost response adapter. No request construction or credentials. */
(function(root) {
  "use strict";
  const pick=(v,keys)=>Object.fromEntries(keys.filter(k=>v?.[k]!==undefined).map(k=>[k,v[k]]));
  function normalize(data, observedAt=Date.now()) {
    if (data?.success!==true || !data.data || typeof data.data.productId!=="string"
        || !/^\d{6,24}$/.test(data.data.productId)) return null;
    const d=data.data;
    const attrs=(d.productAttrs || []).filter(a=>a.showInDetail===true).map(a=>({
      ...pick(a,["attrName","aliasName"]),values:(a.attrVals || []).map(v=>pick(v,["itemName","attrValue"]))}));
    const report=d.reportTabInfo;
    const groups=report?.groupList?.map(g=>({
      ...pick(g,["groupName","tabId","groupEleSize"]),elements:(g.groupElementList || []).map(e=>({
        role:{...pick(e.genshinImpactRoleDTO,["roleName","roleLevel","roleRarity","mingZuo","specializedWeapon"]),
          equipped_weapon:pick(e.genshinImpactRoleDTO?.weaponDTO,["weaponName","weaponLevel","weaponRarity","weaponRefineNum"])},
        weapon:pick(e.genshinImpactWeaponDTO,["weaponName","weaponLevel","weaponRarity","weaponRefineNum"]),
        map:pick(e.genshinImpactMapDTO,["mapName","explorePlan"]),
        item:pick(e.elementDTO,["name","count","level","assetsNum"])}))}));
    return {schema_version:1,source:"detailPost",observed_at:new Date(observedAt).toISOString(),
      product:{id:d.productId,game_id:d.gameId,price_fen:d.price,status:d.status,
        title:d.productName,attrs,report_attrs:(d.reportTitleAttr || []).map(a=>pick(a,["attrName","attrValue"])),
        report:report ? {tabs:(report.reportTabs || []).map(t=>pick(t,["categoryId","categoryName"])),groups:groups || []} : null,
        remark:typeof d.sellerRemark==="string" && !/(?:https?:|\b\d{11}\b|[\w.+-]+@[\w.-]+)/i.test(d.sellerRemark) ? d.sellerRemark : null,guarantee:d.guarantee, favorites:d.collectCount,published_at:d.createTime,published_text:d.shelveUpTimeText}};
  }
  root.PXB7_DETAIL_DATA={normalize};
  if (typeof module!=="undefined") module.exports=root.PXB7_DETAIL_DATA;
})(typeof globalThis!=="undefined" ? globalThis : window);
