"""requiem.dashboard.page — the single self-contained dashboard HTML page.

Held as a module string (not a static file) so there is no filesystem-path
resolution at runtime and the server stays a pure import. Vanilla JS + ``fetch``,
no framework, no build step, no CDN (ADR-0019). The page polls ``/api/*``.
"""
from __future__ import annotations

PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>requiem dashboard</title>
<style>
  :root {
    --bg:#0e1116; --panel:#161b22; --line:#2a2f37; --fg:#d7dde5; --dim:#8b96a5;
    --ok:#3fb950; --fail:#f85149; --gate:#39c5cf; --warn:#d29922; --accent:#6ea8ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         background:var(--bg); color:var(--fg); }
  header { padding:12px 18px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:14px; }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.5px; }
  header .dim { color:var(--dim); font-size:12px; }
  header .spacer { flex:1; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
           border-radius:6px; padding:5px 10px; cursor:pointer; font:inherit; }
  button:hover { border-color:var(--accent); }
  main { display:grid; grid-template-columns: 340px 1fr; gap:0; height:calc(100vh - 49px); }
  #left { border-right:1px solid var(--line); overflow:auto; }
  #right { overflow:auto; padding:0 18px 40px; }
  .section-title { font-size:11px; text-transform:uppercase; letter-spacing:1px;
                   color:var(--dim); padding:12px 14px 6px; position:sticky; top:0;
                   background:var(--bg); }
  .run, .gate { padding:9px 14px; border-bottom:1px solid var(--line); cursor:pointer; }
  .run:hover, .gate:hover { background:var(--panel); }
  .run.active { background:var(--panel); border-left:3px solid var(--accent); padding-left:11px; }
  .run .id { color:var(--fg); }
  .run .meta { color:var(--dim); font-size:12px; display:flex; gap:8px; flex-wrap:wrap; }
  .badge { font-size:11px; padding:1px 7px; border-radius:10px; border:1px solid var(--line); }
  .b-Running { color:var(--accent); } .b-Completed { color:var(--ok); }
  .b-Failed { color:var(--fail); } .b-Cancelled { color:var(--dim); }
  .b-Suspended, .b-Needshuman { color:var(--gate); }
  .b-Corrupt { color:var(--warn); }
  .gate { border-left:3px solid var(--gate); }
  .gate .prompt { color:var(--fg); margin:3px 0; }
  .gate .opts { color:var(--gate); font-size:12px; }
  #detail h2 { font-size:15px; margin:16px 0 4px; }
  #detail .sub { color:var(--dim); font-size:12px; margin-bottom:12px; }
  .gatebar { background:#10242a; border:1px solid var(--gate); border-radius:8px;
             padding:10px 12px; margin:10px 0; }
  .gatebar .opts { color:var(--gate); }
  .gatebar .resolve { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; }
  .rbtn { background:#10242a; color:var(--gate); border:1px solid var(--gate);
          border-radius:6px; padding:4px 12px; cursor:pointer; font:inherit; }
  .rbtn:hover { background:var(--gate); color:#08191d; }
  .rbtn:disabled { opacity:.5; cursor:default; }
  .gatebar .rhint { color:var(--dim); font-size:12px; margin-top:7px; }
  .gatebar code { color:var(--accent); }
  .policy { background:#161616; border:1px solid #2a2a2a; border-radius:8px;
            padding:10px 12px; margin:10px 0; font-size:13px; }
  .policy .ptitle { font-weight:600; margin-bottom:6px; }
  .policy .prow { margin:3px 0; }
  .policy .plabel { color:var(--dim); display:inline-block; min-width:130px; }
  .policy .dim, .dim { color:var(--dim); }
  .ev { display:grid; grid-template-columns: 30px 150px 1fr 150px; gap:8px;
        padding:3px 0; border-bottom:1px solid #1b2027; font-size:13px; }
  .ev .g { text-align:center; } .ev .k { color:var(--dim); }
  .ev .s { color:var(--fg); white-space:pre-wrap; word-break:break-word; }
  .ev .t { color:var(--dim); text-align:right; font-size:12px; }
  .empty { color:var(--dim); padding:24px 14px; }
  .corrupt { color:var(--warn); padding:10px 0; }
  a { color:var(--accent); text-decoration:none; }
</style>
</head>
<body>
<header>
  <h1>requiem</h1><span class="dim">dashboard · read-only</span>
  <span class="spacer"></span>
  <span id="status" class="dim"></span>
  <button id="refresh">refresh</button>
  <label class="dim"><input type="checkbox" id="auto" checked> auto</label>
</header>
<main>
  <div id="left">
    <div class="section-title">Pending gates</div>
    <div id="gates"></div>
    <div class="section-title">Runs</div>
    <div id="runs"></div>
  </div>
  <div id="right"><div id="detail"><div class="empty">Select a run.</div></div></div>
</main>
<script>
const $ = (s,r=document)=>r.querySelector(s);
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const cls = s => "b-"+String(s||"").replace(/\s+/g,"");
let selected = null;

async function getJSON(u){ const r = await fetch(u); if(!r.ok) throw new Error(r.status); return r.json(); }

function runRow(r){
  const d = document.createElement("div");
  d.className = "run" + (r.run_id===selected?" active":"");
  d.innerHTML = `<div class="id">${esc(r.run_id)}</div>
    <div class="meta"><span class="badge ${cls(r.status)}">${esc(r.status)}</span>
    <span>${esc(r.workflow||"—")}</span><span>${r.events} ev</span>
    ${r.gate_open?'<span class="badge b-Suspended">gate</span>':''}</div>`;
  d.onclick = ()=>{ selected = r.run_id; openRun(r.run_id); paintRuns(lastRuns); };
  return d;
}

let lastRuns = [];
function paintRuns(runs){
  lastRuns = runs;
  const c = $("#runs"); c.innerHTML="";
  if(!runs.length){ c.innerHTML='<div class="empty">No runs under this log-dir.</div>'; return; }
  runs.forEach(r=>c.appendChild(runRow(r)));
}

function paintGates(gates){
  const c = $("#gates"); c.innerHTML="";
  if(!gates.length){ c.innerHTML='<div class="empty">None — nothing awaiting a human.</div>'; return; }
  gates.forEach(g=>{
    const d=document.createElement("div"); d.className="gate";
    d.innerHTML=`<div class="id">${esc(g.run_id)} <span class="dim">· ${esc(g.node||"")}</span></div>
      <div class="prompt">${esc(g.prompt)}</div>
      <div class="opts">options: ${(g.options||[]).map(esc).join(" · ")||"—"}</div>`;
    d.onclick=()=>{ selected=g.run_id; openRun(g.run_id); paintRuns(lastRuns); };
    c.appendChild(d);
  });
}

async function openRun(id){
  const d = $("#detail");
  try {
    const r = await getJSON("/api/runs/"+encodeURIComponent(id));
    let html = `<h2>${esc(r.run_id)} <span class="badge ${cls(r.status)}">${esc(r.status)}</span></h2>
      <div class="sub">${esc(r.workflow||"—")} · final_node: ${esc(r.final_node||"—")} · started ${esc(r.started||"—")}</div>`;
    if(r.corrupt) html += `<div class="corrupt">⚠ log corrupt: ${esc(r.corrupt)}</div>`;
    if(r.gate) html += `<div class="gatebar">🚦 <b>${esc(r.gate.node||"")}</b> — ${esc(r.gate.prompt)}
      <div class="opts">options: ${(r.gate.options||[]).map(esc).join(" · ")}</div>
      <div class="resolve">${(r.gate.options||[]).map(o=>`<button class="rbtn" data-run="${esc(r.run_id)}" data-choice="${esc(o)}">resolve: ${esc(o)}</button>`).join(" ")}</div>
      <div class="rhint">Appends a guarded <code>gate_resolved</code> event — then run <code>requiem resume ${esc(r.run_id)}</code> to continue.</div></div>`;
    if(r.policy){
      const p = r.policy;
      const row = (label, arr) => `<div class="prow"><span class="plabel">${esc(label)}</span> ${(arr||[]).length ? (arr||[]).map(esc).join(" · ") : "<span class=\\"dim\\">—</span>"}</div>`;
      const aliases = p.type_aliases && Object.keys(p.type_aliases).length
        ? Object.entries(p.type_aliases).map(([k,v])=>`${esc(k)}→${esc(v)}`).join(" · ") : "—";
      html += `<div class="policy"><div class="ptitle">⚙ tier policy <span class="dim">(process config)</span></div>
        ${row("root parent types", p.root_parent_types)}
        ${row("decomposable", p.decomposable_types)}
        ${row("implementable", p.implementable_types)}
        <div class="prow"><span class="plabel">aliases</span> ${aliases}</div>
        ${p.source?`<div class="prow"><span class="plabel">source</span> <code>${esc(p.source)}</code>${p.sha256?` <span class="dim">sha ${esc((p.sha256||"").slice(0,12))}</span>`:""}</div>`:""}</div>`;
    }
    html += (r.timeline||[]).map(e=>`<div class="ev"><span class="g">${esc(e.glyph)}</span>
      <span class="k">${esc(e.kind)}</span><span class="s">${esc(e.summary)}</span>
      <span class="t">${esc((e.ts||"").slice(11,19))}</span></div>`).join("");
    d.innerHTML = html;
  } catch(e){ d.innerHTML = `<div class="empty">Could not load ${esc(id)} (${esc(e.message)}).</div>`; }
}

async function refresh(){
  try{
    const [runs, gates] = await Promise.all([getJSON("/api/runs"), getJSON("/api/gates")]);
    paintRuns(runs.runs||[]); paintGates(gates.gates||[]);
    if(selected) await openRun(selected);
    $("#status").textContent = `${(runs.runs||[]).length} runs · ${(gates.gates||[]).length} gates · ${new Date().toLocaleTimeString()}`;
  }catch(e){ $("#status").textContent = "refresh failed: "+e.message; }
}

async function resolveGate(runId, choice, btn){
  if(!confirm(`Resolve gate for ${runId} with "${choice}"?\n\nThis appends a gate_resolved event. Continue the run with: requiem resume ${runId}`)) return;
  const prev = btn ? btn.textContent : null;
  if(btn){ btn.disabled = true; btn.textContent = "resolving…"; }
  try {
    const r = await fetch(`/api/gates/${encodeURIComponent(runId)}/resolve`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({choice})
    });
    const data = await r.json().catch(()=>({}));
    if(!r.ok){ throw new Error(data.error || ("HTTP "+r.status)); }
    $("#status").textContent = `resolved ${runId} → ${choice} (event #${data.event_id}) · run: requiem resume ${runId}`;
    await refresh();
  } catch(e){
    alert("Resolution refused: "+e.message);
    if(btn){ btn.disabled = false; btn.textContent = prev; }
  }
}

// Delegated: resolve buttons are re-rendered on every openRun().
$("#detail").addEventListener("click", ev=>{
  const b = ev.target.closest(".rbtn");
  if(b) resolveGate(b.dataset.run, b.dataset.choice, b);
});

$("#refresh").onclick = refresh;
let timer = setInterval(()=>{ if($("#auto").checked) refresh(); }, 4000);
refresh();
</script>
</body>
</html>
"""
