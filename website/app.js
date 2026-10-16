let cruises = [];
const $ = s => document.querySelector(s);
const money = (v,currency="AUD") => v == null ? "—" : new Intl.NumberFormat("en-AU",{style:"currency",currency:currency||"AUD",maximumFractionDigits:0}).format(v);
const pct = v => v == null ? "—" : `${v.toFixed(1)}%`;
const cabinLabels = {interior:"Interior",oceanview:"Oceanview",balcony:"Balcony",mini_suite:"Mini-Suite",suite:"Suite"};

function selectedPorts(){ return [...document.querySelectorAll("#ports input:checked")].map(x=>x.value); }
function cabin(){ return $("#cabin").value; }
function packageAddon(c){
  const p=$("#farePackage").value;
  if(p==="plus") return (c.plus_addon_per_night ?? 79) * (c.nights||0);
  if(p==="premier") return (c.premier_addon_per_night ?? 129) * (c.nights||0);
  return 0;
}
function basePriceFor(c){
  const cab=cabin();
  return $("#passengers").value === "1" ? c[`one_guest_${cab}`] : c[`two_guest_${cab}_pp`];
}
function priceFor(c){
  const base=basePriceFor(c);
  return base==null ? null : base + packageAddon(c);
}
function perNight(c){ const p=priceFor(c); return p!=null && c.nights ? p/c.nights : null; }
function currentTotal(c){ const p=priceFor(c); return p==null?null:($("#passengers").value==="1"?p:p*2); }
function historyBase(h){ const cab=cabin(); return $("#passengers").value==="1" ? h[`one_guest_${cab}`] : h[`two_guest_${cab}_pp`]; }
function historyPrices(c){
  return (c.history||[]).map(h=>{
    const base=historyBase(h); return base==null?null:base+packageAddon(c);
  }).filter(v=>v!=null);
}
function historicalLow(c){ const a=historyPrices(c); return a.length?Math.min(...a):null; }
function priceChange(c){ const a=historyPrices(c); return a.length>1?a.at(-1)-a.at(-2):null; }
function soloPremium(c){
  const cab=cabin(), one=c[`one_guest_${cab}`], two=c[`two_guest_${cab}_pp`];
  return one!=null&&two!=null?((one/two)-1)*100:null;
}
function passes(c){
  if(!selectedPorts().includes(c.departure_port)) return false;
  if(c.nights && c.nights < (+$("#minNights").value||0)) return false;
  if(c.nights && c.nights > (+$("#maxNights").value||999)) return false;
  if($("#dateFrom").value && c.departure_date < $("#dateFrom").value) return false;
  if($("#dateTo").value && c.departure_date > $("#dateTo").value) return false;
  if($("#ship").value && c.ship !== $("#ship").value) return false;
  if($("#available").checked && priceFor(c)==null) return false;
  if($("#dropped").checked && !(priceChange(c)<0)) return false;
  const max=+$("#maxPrice").value;
  if(max && (priceFor(c)==null || priceFor(c)>max)) return false;
  return true;
}
function sortRows(rows){
  const s=$("#sort").value;
  return rows.sort((a,b)=>{
    if(s==="price") return (priceFor(a)??1e15)-(priceFor(b)??1e15);
    if(s==="date") return (a.departure_date||"9999").localeCompare(b.departure_date||"9999");
    if(s==="drop") return (priceChange(a)??1e15)-(priceChange(b)??1e15);
    if(s==="lengthDesc") return (b.nights??0)-(a.nights??0);
    if(s==="lengthAsc") return (a.nights??1e15)-(b.nights??1e15);
    if(s==="solo") return (soloPremium(a)??1e15)-(soloPremium(b)??1e15);
    return (perNight(a)??1e15)-(perNight(b)??1e15);
  });
}
function dateFmt(s){ return !s?"":new Date(s+"T12:00:00").toLocaleDateString("en-AU",{day:"numeric",month:"short",year:"numeric"}); }
function packageLabel(){ return {standard:"Princess Standard",plus:"Princess Plus",premier:"Princess Premier"}[$("#farePackage").value]; }
function card(c){
  const p=priceFor(c), ppn=perNight(c), total=currentTotal(c), cur=c.currency||"AUD", low=historicalLow(c), change=priceChange(c), solo=soloPremium(c);
  const changeText = change==null?"":`<span class="badge ${change<0?"drop":"rise"}">${change<0?"↓":"↑"} ${money(Math.abs(change),cur)} since previous check</span>`;
  const lowText = p!=null&&low!=null&&p<=low ? `<span class="badge low">Historical low</span>` : "";
  const selectedAvailable = p!=null;
  const titleDate = c.departure_date ? ` - ${dateFmt(c.departure_date)}` : "";
  return `<article class="card">
    <div>
      <h2>${c.name||"Princess cruise"}${titleDate}</h2>
      <div class="meta">${c.ship||"Ship TBA"} · ${c.nights||"?"} nights · Depart ${c.departure_port||"?"}</div>
      <div class="route">${(c.itinerary||"").slice(0,310)}</div>
      <div class="badges"><span class="badge">${selectedAvailable?"Available":"Selected cabin unavailable"}</span><span class="badge">${cur}</span><span class="badge">${cabinLabels[cabin()]}</span><span class="badge">${packageLabel()}</span>${changeText}${lowText}</div>
    </div>
    <div class="pricing">
      <div class="price">${p==null?"Unavailable":`${money(p,cur)} pp`}</div>
      <div class="pernight">${ppn==null?"":`${money(ppn,cur)} pp / night`}</div>
      <div class="secondary">
        ${$("#passengers").value==="2" && total!=null ? `2-person total: <b>${money(total,cur)}</b><br>` : ""}
        Standard ${cabinLabels[cabin()]}: <b>${money(basePriceFor(c),cur)}</b><br>
        ${$("#farePackage").value!=="standard"&&p!=null?`Package add-on: <b>${money(packageAddon(c),cur)} pp</b><br>`:""}
        Solo premium: <b>${pct(solo)}</b><br>
        Historical low: <b>${money(low,cur)}</b>
      </div>
    </div>
    <div class="actions"><a class="book" href="${c.url||"https://www.princess.com"}" target="_blank" rel="noopener">View on Princess</a><button class="history" onclick="showHistory('${c.cruise_id.replaceAll("'","")}')">Price history</button></div>
  </article>`;
}
function render(){
  let rows=sortRows(cruises.filter(passes));
  $("#resultCount").textContent=`${rows.length} cruise${rows.length===1?"":"s"}`;
  $("#results").innerHTML=rows.length?rows.map(card).join(""):`<div class="empty">No cruises match the selected filters or cabin availability.</div>`;
  const vals=rows.map(priceFor).filter(v=>v!=null), days=rows.map(perNight).filter(v=>v!=null), drops=rows.filter(c=>priceChange(c)<0).length;
  $("#summary").innerHTML=`<div class="stat"><b>${rows.length}</b><span>Matching cruises</span></div><div class="stat"><b>${money(days.length?Math.min(...days):null,"AUD")}</b><span>Best price per night</span></div><div class="stat"><b>${money(vals.length?Math.min(...vals):null,"AUD")}</b><span>Lowest fare</span></div><div class="stat"><b>${drops}</b><span>Current price drops</span></div>`;
}
window.showHistory=function(id){
  const c=cruises.find(x=>x.cruise_id===id), cur=c.currency||"AUD", h=(c.history||[]).slice().reverse();
  $("#historyTitle").textContent=`Price history — ${c.name||"Princess cruise"} — ${cabinLabels[cabin()]} — ${packageLabel()}`;
  $("#historyBody").innerHTML=h.length?`<table class="history-table"><thead><tr><th>Checked</th><th>Standard base</th><th>Selected fare</th></tr></thead><tbody>${h.map(x=>{const base=historyBase(x), selected=base==null?null:base+packageAddon(c);return `<tr><td>${new Date(x.checked_at).toLocaleString("en-AU")}</td><td>${money(base,x.currency||cur)}</td><td>${money(selected,x.currency||cur)}</td></tr>`}).join("")}</tbody></table>`:"No historical observations yet.";
  $("#historyDialog").showModal();
}
function reset(){
  document.querySelectorAll("#ports input").forEach(x=>x.checked=true);
  $("#passengers").value="2"; $("#cabin").value="interior"; $("#farePackage").value="standard";
  $("#minNights").value=1; $("#maxNights").value=""; $("#dateFrom").value=""; $("#dateTo").value=""; $("#maxPrice").value="";
  $("#ship").value=""; $("#sort").value="dayprice"; $("#available").checked=false; $("#dropped").checked=false; render();
}
async function init(){
  const res=await fetch("../data/latest.json",{cache:"no-store"}); cruises=await res.json();
  const ships=[...new Set(cruises.map(c=>c.ship).filter(Boolean))].sort();
  $("#ship").innerHTML=`<option value="">All ships</option>`+ships.map(s=>`<option>${s}</option>`).join("");
  const timestamps=cruises.map(c=>c.checked_at).filter(Boolean).sort(); $("#updated").textContent=timestamps.length?`Last data: ${new Date(timestamps.at(-1)).toLocaleString("en-AU")}`:"";
  document.querySelectorAll("input,select").forEach(el=>el.addEventListener("change",render));
  $("#maxPrice").addEventListener("input",render); $("#reset").addEventListener("click",reset); $("#closeHistory").addEventListener("click",()=>$("#historyDialog").close()); render();
}
init().catch(err=>{console.error(err);$("#results").innerHTML=`<div class="empty"><b>Could not load cruise data.</b></div>`;});
