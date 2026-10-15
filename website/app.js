let cruises = [];
const $ = s => document.querySelector(s);
const money = v => v == null ? "—" : new Intl.NumberFormat("en-AU",{style:"currency",currency:"AUD",maximumFractionDigits:0}).format(v);
const pct = v => v == null ? "—" : `${v.toFixed(1)}%`;

function selectedPorts(){
  return [...document.querySelectorAll("#ports input:checked")].map(x=>x.value);
}
function priceFor(c){
  return $("#passengers").value === "1" ? c.one_guest_price : c.two_guest_pp_price;
}
function perNight(c){
  const p=priceFor(c); return p && c.nights ? p/c.nights : null;
}
function currentTotal(c){
  const p=priceFor(c);
  return $("#passengers").value === "1" ? p : (c.two_guest_total ?? (p ? p*2 : null));
}
function passes(c){
  if(!selectedPorts().includes(c.departure_port)) return false;
  if(c.nights && c.nights < (+$("#minNights").value||0)) return false;
  if(c.nights && c.nights > (+$("#maxNights").value||999)) return false;
  if($("#dateFrom").value && c.departure_date < $("#dateFrom").value) return false;
  if($("#dateTo").value && c.departure_date > $("#dateTo").value) return false;
  if($("#ship").value && c.ship !== $("#ship").value) return false;
  if($("#available").checked && !c.available) return false;
  if($("#dropped").checked && !(c.price_change < 0)) return false;
  const max=+$("#maxPrice").value;
  if(max && (priceFor(c)==null || priceFor(c)>max)) return false;
  return true;
}
function sortRows(rows){
  const s=$("#sort").value;
  return rows.sort((a,b)=>{
    if(s==="price") return (priceFor(a)??1e15)-(priceFor(b)??1e15);
    if(s==="date") return (a.departure_date||"9999").localeCompare(b.departure_date||"9999");
    if(s==="drop") return (a.price_change??1e15)-(b.price_change??1e15);
    if(s==="lengthDesc") return (b.nights??0)-(a.nights??0);
    if(s==="lengthAsc") return (a.nights??1e15)-(b.nights??1e15);
    if(s==="solo") return (a.solo_premium_pct??1e15)-(b.solo_premium_pct??1e15);
    return (perNight(a)??1e15)-(perNight(b)??1e15);
  });
}
function dateFmt(s){
  if(!s) return "Date unavailable";
  return new Date(s+"T12:00:00").toLocaleDateString("en-AU",{day:"numeric",month:"short",year:"numeric"});
}
function card(c){
  const p=priceFor(c), ppn=perNight(c), total=currentTotal(c);
  const isLow = p != null && c.historical_low != null && p <= c.historical_low;
  const changeClass = c.price_change < 0 ? "drop" : "rise";
  const changeText = c.price_change == null ? "" :
    `<span class="badge ${changeClass}">${c.price_change<0?"↓":"↑"} ${money(Math.abs(c.price_change))} since previous check</span>`;
  const lowText = isLow ? `<span class="badge low">Historical low</span>` : "";
  const solo = c.solo_premium_pct != null ? `Solo premium ${pct(c.solo_premium_pct)}` : "Solo premium unavailable";
  return `<article class="card">
    <div>
      <h2>${c.name||"Princess cruise"}</h2>
      <div class="meta">${c.ship||"Ship TBA"} · ${dateFmt(c.departure_date)} · ${c.nights||"?"} nights · Depart ${c.departure_port||"?"}</div>
      <div class="route">${(c.itinerary||"").slice(0,310)}</div>
      <div class="badges">
        <span class="badge">${c.available?"Available":"Unavailable"}</span>
        ${changeText}${lowText}
      </div>
    </div>
    <div class="pricing">
      <div class="price">${money(p)}${p!=null?" pp":""}</div>
      <div class="pernight">${ppn==null?"":`${money(ppn)} pp / night`}</div>
      <div class="secondary">
        ${$("#passengers").value==="2" ? `2-person total: <b>${money(total)}</b><br>` : ""}
        1 guest: <b>${money(c.one_guest_price)}</b><br>
        2 guests pp: <b>${money(c.two_guest_pp_price)}</b><br>
        ${solo}<br>
        Historical low: <b>${money(c.historical_low)}</b>
      </div>
    </div>
    <div class="actions">
      <a class="book" href="${c.url||"https://www.princess.com"}" target="_blank" rel="noopener">View on Princess</a>
      <button class="history" onclick="showHistory('${c.cruise_id.replaceAll("'","")}')">Price history</button>
    </div>
  </article>`;
}
function render(){
  let rows=sortRows(cruises.filter(passes));
  $("#resultCount").textContent=`${rows.length} cruise${rows.length===1?"":"s"}`;
  $("#results").innerHTML=rows.length?rows.map(card).join(""):`<div class="empty">No cruises match the selected filters.</div>`;
  const dayPrices=rows.map(perNight).filter(v=>v!=null);
  const prices=rows.map(priceFor).filter(v=>v!=null);
  const drops=rows.filter(c=>c.price_change<0).length;
  $("#summary").innerHTML=`
    <div class="stat"><b>${rows.length}</b><span>Matching cruises</span></div>
    <div class="stat"><b>${money(dayPrices.length?Math.min(...dayPrices):null)}</b><span>Best price per night</span></div>
    <div class="stat"><b>${money(prices.length?Math.min(...prices):null)}</b><span>Lowest fare</span></div>
    <div class="stat"><b>${drops}</b><span>Current price drops</span></div>`;
}
window.showHistory=function(id){
  const c=cruises.find(x=>x.cruise_id===id);
  $("#historyTitle").textContent=`Price history — ${c.name||"Princess cruise"}`;
  const h=(c.history||[]).slice().reverse();
  $("#historyBody").innerHTML=h.length?`<table class="history-table"><thead><tr><th>Checked</th><th>1 guest</th><th>2 guests pp</th></tr></thead><tbody>
  ${h.map(x=>`<tr><td>${new Date(x.checked_at).toLocaleString("en-AU")}</td><td>${money(x.one_guest_price)}</td><td>${money(x.two_guest_pp_price)}</td></tr>`).join("")}
  </tbody></table>`:"No historical observations yet.";
  $("#historyDialog").showModal();
}
function reset(){
  document.querySelectorAll("#ports input").forEach(x=>x.checked=true);
  $("#passengers").value="2"; $("#minNights").value=1; $("#maxNights").value=60;
  $("#dateFrom").value=""; $("#dateTo").value=""; $("#maxPrice").value="";
  $("#ship").value=""; $("#sort").value="dayprice"; $("#available").checked=true; $("#dropped").checked=false;
  render();
}
async function init(){
  const res=await fetch("../data/latest.json",{cache:"no-store"});
  cruises=await res.json();
  const ships=[...new Set(cruises.map(c=>c.ship).filter(Boolean))].sort();
  $("#ship").innerHTML=`<option value="">All ships</option>`+ships.map(s=>`<option>${s}</option>`).join("");
  const timestamps=cruises.map(c=>c.checked_at).filter(Boolean).sort();
  $("#updated").textContent=timestamps.length?`Last data: ${new Date(timestamps.at(-1)).toLocaleString("en-AU")}`:"";
  document.querySelectorAll("input,select").forEach(el=>el.addEventListener("change",render));
  $("#maxPrice").addEventListener("input",render);
  $("#reset").addEventListener("click",reset);
  $("#closeHistory").addEventListener("click",()=>$("#historyDialog").close());
  render();
}
init().catch(err=>{
  console.error(err);
  $("#results").innerHTML=`<div class="empty"><b>Could not load cruise data.</b><br>Start a local web server from the project folder with <code>python -m http.server 8000</code>.</div>`;
});
