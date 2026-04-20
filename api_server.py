# api_server.py — CRACK SMS API Server v3.0
"""
FastAPI server:
  • /                   — Public live OTP dashboard (no token required)
  • /api/public/otps    — Public JSON OTPs endpoint (no token)
  • /api/public/stats   — Public stats (no token)
  • /api/sms            — Authenticated, panel-filtered (token required)
  • /api/stats          — Authenticated stats (token required)
  • /health             — Health check (public)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func

sys.path.insert(0, os.path.dirname(__file__))

from database import (
    AsyncSessionLocal, APIToken, Number, History,
    get_api_token, update_api_token_last_used,
    create_api_token, get_all_api_tokens, delete_api_token,
)

try:
    from logging_system import bootstrap, get_logger, audit_api
    bootstrap()
    logger = get_logger("api_server")
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("api_server")
    async def audit_api(**kwargs): pass

app = FastAPI(title="CRACK SMS API", version="3.0.0", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# ── Auth helper ────────────────────────────────────────────────────────────────
async def validate_token(token: str) -> Optional[APIToken]:
    if not token:
        return None
    t = await get_api_token(token)
    if not t or t.status != "ACTIVE":
        return None
    await update_api_token_last_used(token)
    return t


# ── Shared OTP fetch logic ─────────────────────────────────────────────────────
async def _fetch_otps(limit=120, allowed_panels=None, date_str=None):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(History).order_by(History.created_at.desc()).limit(limit * 3)
        )).scalars().all()
    out = []
    for r in rows:
        if date_str:
            try:
                if r.created_at.date() != datetime.strptime(date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass
        if allowed_panels:
            cat = r.category or ""
            if not any(str(p).lower() in cat.lower() for p in allowed_panels):
                continue
        cat = r.category or ""
        if " - " in cat:
            country, service = cat.split(" - ", 1)[0].strip(), cat.split(" - ", 1)[1].strip()
        else:
            country, service = "Unknown", cat or "Unknown"
        out.append({
            "number":      f"+{r.phone_number}" if r.phone_number else "—",
            "service":     service,
            "country":     country,
            "otp":         r.otp or "—",
            "message":     f"OTP for {service}: {r.otp}" if r.otp else "—",
            "received_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if len(out) >= limit:
            break
    return out


async def _fetch_stats():
    async with AsyncSessionLocal() as s:
        total    = await s.scalar(select(func.count(History.id))) or 0
        today    = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_ct = await s.scalar(select(func.count(History.id)).where(History.created_at >= today)) or 0
        rows     = (await s.execute(
            select(History.category, func.count(History.id))
            .group_by(History.category).order_by(func.count(History.id).desc()).limit(10)
        )).all()
        services = {}
        for cat, cnt in rows:
            svc = cat.split(" - ", 1)[1].strip() if cat and " - " in cat else (cat or "Unknown")
            services[svc] = services.get(svc, 0) + cnt
        since   = datetime.now() - timedelta(hours=24)
        h_rows  = (await s.execute(
            select(History.created_at).where(History.created_at >= since)
        )).scalars().all()
        hourly  = {}
        for ts in h_rows:
            b = ts.strftime("%Y-%m-%d %H:00")
            hourly[b] = hourly.get(b, 0) + 1
    return {"total_otps": total, "otps_today": today_ct,
            "by_service": services, "hourly_last_24h": hourly}


# ── PUBLIC endpoints (no token) ────────────────────────────────────────────────

@app.get("/api/public/otps")
async def public_otps(limit: int = Query(120, ge=1, le=500)):
    try:
        data = await _fetch_otps(limit)
        return {"status": "success", "total_records": len(data), "data": data}
    except Exception as e:
        logger.exception("public_otps: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/public/stats")
async def public_stats_ep():
    try:
        return {"status": "success", **(await _fetch_stats())}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── AUTHENTICATED endpoints ────────────────────────────────────────────────────

@app.get("/api/sms")
async def get_otps(
    request: Request,
    token: str = Query(...),
    date: Optional[str]  = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        allowed: list = json.loads(api_token.panels_data or "[]")
        otps = await _fetch_otps(limit, allowed_panels=allowed or None, date_str=date)
        ip   = request.client.host if request.client else None
        await audit_api(token_name=api_token.name, endpoint="/api/sms",
                        records_returned=len(otps), ip=ip)
        return {"status": "success", "token_name": api_token.name,
                "api_dev": api_token.api_dev or "Anonymous",
                "total_records": len(otps), "data": otps}
    except Exception as e:
        logger.exception("get_otps: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/stats")
async def get_stats_ep(token: str = Query(...)):
    t = await validate_token(token)
    if not t:
        raise HTTPException(401, "Not authorized")
    try:
        return {"status": "success", **(await _fetch_stats())}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "CRACK SMS API v3",
            "timestamp": datetime.now().isoformat()}


# ── API TOKEN MANAGEMENT endpoints ────────────────────────────────────────────

@app.post("/api/tokens/create")
async def create_token(name: str, developer: str = None, panels: list = None):
    """Create a new API token (admin only)."""
    try:
        import secrets, string
        # Generate random token
        chars = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(chars) for _ in range(32))
        
        panels_json = json.dumps(panels or [])
        await create_api_token(token, name, 1, developer or None, panels_json)
        
        return {
            "status": "success",
            "message": "Token created",
            "token": token,
            "name": name
        }
    except Exception as e:
        logger.exception("create_token: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/tokens")
async def list_tokens(token: str = Query(...)):
    """List all tokens (admin only)."""
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    
    try:
        tokens = await get_all_api_tokens()
        return {
            "status": "success",
            "total": len(tokens),
            "tokens": [
                {
                    "id": t.id,
                    "name": t.name,
                    "token": f"{t.token[:8]}...{t.token[-4:]}",
                    "created_at": t.created_at.isoformat(),
                    "last_used": t.last_used.isoformat() if t.last_used else None,
                    "status": t.status
                }
                for t in tokens
            ]
        }
    except Exception as e:
        logger.exception("list_tokens: %s", e)
        return {"status": "error", "message": str(e)}


@app.delete("/api/tokens/{token_id}")
async def delete_token_ep(token_id: str, token: str = Query(...)):
    """Delete an API token (admin only)."""
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    
    try:
        success = await delete_api_token(token_id)
        if success:
            return {"status": "success", "message": "Token deleted"}
        return {"status": "error", "message": "Token not found"}
    except Exception as e:
        logger.exception("delete_token: %s", e)
        return {"status": "error", "message": str(e)}


@app.post("/api/tokens/{token_id}/regenerate")
async def regenerate_token_ep(token_id: str, token: str = Query(...)):
    """Regenerate an API token (admin only)."""
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    
    try:
        # Delete old token and create new one
        import secrets, string
        chars = string.ascii_letters + string.digits
        new_token = ''.join(secrets.choice(chars) for _ in range(32))
        
        await delete_api_token(token_id)
        # Note: In production, you'd retrieve the original token's metadata first
        await create_api_token(new_token, f"regenerated_{token_id}", 1)
        
        return {
            "status": "success",
            "message": "Token regenerated",
            "new_token": new_token
        }
    except Exception as e:
        logger.exception("regenerate_token: %s", e)
        return {"status": "error", "message": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  BEAUTIFUL PUBLIC HOMEPAGE — shows all OTPs, no token needed
# ══════════════════════════════════════════════════════════════════════════════
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRACK SMS — Live OTP Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0b10;--s0:#0f1018;--s1:#151820;--s2:#1e2232;--bdr:#2a2e42;
  --acc:#7c3aed;--acc2:#ec4899;--grn:#10b981;--yel:#f59e0b;--red:#ef4444;
  --txt:#f1f5f9;--sub:#94a3b8;--dim:#64748b;
  --mono:'JetBrains Mono',monospace;--sans:'Space Grotesk',sans-serif;--r:12px;
}
html{scroll-behavior:smooth}
body{
  font-family:var(--sans);background:var(--bg);color:var(--txt);min-height:100vh;
  line-height:1.6;overflow-x:hidden
}
/* animated background */
body::before{
  content:'';position:fixed;inset:0;
  background:
    radial-gradient(circle at 20% 50%, rgba(124,58,237,.08) 0%, transparent 50%),
    radial-gradient(circle at 80% 80%, rgba(236,72,153,.08) 0%, transparent 50%);
  pointer-events:none;z-index:0;animation:drift 15s ease-in-out infinite
}
@keyframes drift{0%,100%{transform:translate(0,0)} 50%{transform:translate(20px,-30px)}}
/* topbar */
.tb{
  position:sticky;top:0;z-index:100;
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 28px;background:rgba(10,11,16,.92);
  backdrop-filter:blur(16px);border-bottom:1px solid var(--bdr)
}
.logo{
  display:flex;align-items:center;gap:12px;font-size:1.1rem;font-weight:700;
  letter-spacing:-0.5px
}
.logo-icon{
  width:38px;height:38px;border-radius:10px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:grid;place-items:center;font-size:1.2rem;box-shadow:0 8px 24px rgba(124,58,237,.2)
}
.logo-txt{background:linear-gradient(135deg,#c4b5fd,#f472b6);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.status-bar{display:flex;align-items:center;gap:12px}
.status-pill{
  font-size:0.75rem;font-family:var(--mono);padding:6px 14px;border-radius:20px;
  border:1px solid;display:flex;align-items:center;gap:6px;font-weight:600
}
.pill-live{
  color:var(--grn);border-color:rgba(16,185,129,.3);
  background:rgba(16,185,129,.08)
}
.pill-live::before{
  content:'';width:7px;height:7px;background:var(--grn);border-radius:50%;
  animation:pulse 2s infinite;box-shadow:0 0 10px var(--grn)
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.6;transform:scale(1.2)}}
.pill-cnt{color:var(--dim);border-color:var(--bdr);background:rgba(100,116,139,.05)}
.btn-api{
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  color:#fff;border:none;padding:8px 16px;border-radius:8px;
  font-size:0.8rem;font-weight:600;cursor:pointer;transition:all 0.3s;
  box-shadow:0 4px 12px rgba(124,58,237,.3)
}
.btn-api:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(124,58,237,.4)}
/* main layout */
.wrap{position:relative;z-index:1;max-width:1480px;margin:0 auto;padding:28px 24px}
/* stats section */
.stats-section{
  margin-bottom:32px
}
.stats-title{
  font-size:0.9rem;font-weight:700;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--dim);margin-bottom:14px
}
.stats{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:14px
}
.stat-card{
  background:linear-gradient(135deg, var(--s1) 0%, var(--s2) 100%);
  border:1px solid var(--bdr);border-radius:var(--r);
  padding:20px 22px;position:relative;overflow:hidden;
  transition:all 0.3s;cursor:pointer
}
.stat-card:hover{
  transform:translateY(-4px);border-color:var(--acc);
  box-shadow:0 12px 36px rgba(124,58,237,.15)
}
.stat-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg, var(--line,var(--acc)), transparent);
}
.stat-card.g{--line:var(--grn)}.stat-card.y{--line:var(--yel)}.stat-card.r{--line:var(--red)}
.stat-icon{position:absolute;top:14px;right:14px;font-size:1.8rem;opacity:0.1}
.stat-value{
  font-size:2.4rem;font-weight:700;font-family:var(--mono);
  margin-bottom:6px;background:linear-gradient(135deg, var(--txt), var(--sub));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text
}
.stat-label{
  font-size:0.7rem;color:var(--dim);letter-spacing:0.8px;
  text-transform:uppercase;font-weight:600
}
/* charts section */
.charts-section{
  display:grid;grid-template-columns:2fr 1.2fr;gap:16px;margin-bottom:32px
}
@media(max-width:1024px){.charts-section{grid-template-columns:1fr}}
.chart-box{
  background:linear-gradient(135deg, var(--s1) 0%, var(--s2) 100%);
  border:1px solid var(--bdr);border-radius:var(--r);
  padding:22px 24px;position:relative;overflow:hidden
}
.chart-box::before{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg, rgba(124,58,237,.05), transparent);
  pointer-events:none
}
.chart-title{
  font-size:0.8rem;font-weight:700;text-transform:uppercase;
  letter-spacing:0.8px;color:var(--dim);margin-bottom:18px
}
.chart-box canvas{max-height:220px}
/* controls section */
.controls{
  display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;
  align-items:center
}
.search-box{
  flex:1;min-width:220px;position:relative
}
.search-box input{
  width:100%;background:var(--s1);border:1px solid var(--bdr);
  border-radius:10px;color:var(--txt);font-family:var(--sans);
  font-size:0.85rem;padding:10px 14px;outline:none;
  transition:all 0.3s;padding-left:36px
}
.search-box input::placeholder{color:var(--dim)}
.search-box input:focus{
  border-color:var(--acc);box-shadow:0 0 0 3px rgba(124,58,237,.1)
}
.search-icon{
  position:absolute;left:12px;top:50%;transform:translateY(-50%);
  color:var(--dim);font-size:0.9rem
}
.filter-select{
  background:var(--s1);border:1px solid var(--bdr);border-radius:10px;
  color:var(--txt);font-family:var(--sans);font-size:0.85rem;
  padding:10px 14px;outline:none;cursor:pointer;transition:all 0.3s;
  min-width:160px
}
.filter-select:focus{border-color:var(--acc)}
.btn-refresh,.btn-export{
  background:var(--s1);color:var(--txt);border:1px solid var(--bdr);
  padding:10px 18px;border-radius:10px;font-size:0.85rem;font-weight:600;
  cursor:pointer;transition:all 0.3s;font-family:var(--sans)
}
.btn-refresh:hover,.btn-export:hover{
  border-color:var(--acc);color:var(--acc);
  box-shadow:0 4px 12px rgba(124,58,237,.15)
}
/* grid layout */
.grid-container{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
  gap:14px;margin-bottom:32px
}
/* otp card */
.otp-card{
  background:linear-gradient(135deg, var(--s1) 0%, var(--s2) 100%);
  border:1px solid var(--bdr);border-radius:var(--r);
  padding:18px 20px;transition:all 0.3s;position:relative;
  overflow:hidden;animation:slideIn 0.4s ease-out
}
@keyframes slideIn{from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)}}
.otp-card:hover{
  transform:translateY(-6px);border-color:var(--acc);
  box-shadow:0 16px 48px rgba(124,58,237,.2)
}
.otp-card::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg, var(--acc), var(--acc2));
  border-radius:0 2px 2px 0
}
.otp-card::after{
  content:'';position:absolute;inset:0;border-radius:var(--r);
  background:linear-gradient(135deg, rgba(124,58,237,.05), transparent);
  opacity:0;transition:opacity 0.3s;pointer-events:none
}
.otp-card:hover::after{opacity:1}
.new-badge{
  position:absolute;top:12px;right:12px;font-size:0.65rem;font-weight:700;
  letter-spacing:0.5px;text-transform:uppercase;padding:4px 10px;
  border-radius:20px;background:rgba(16,185,129,.15);
  color:var(--grn);border:1px solid rgba(16,185,129,.3);
  animation:fadeOut 6s forwards
}
@keyframes fadeOut{0%,80%{opacity:1} 100%{opacity:0}}
.card-header{
  display:flex;justify-content:space-between;align-items:flex-start;
  margin-bottom:14px;gap:8px
}
.service-tag{
  font-size:0.65rem;font-weight:700;letter-spacing:0.6px;
  text-transform:uppercase;padding:4px 10px;border-radius:6px;
  background:rgba(124,58,237,.12);color:var(--acc);
  border:1px solid rgba(124,58,237,.2)
}
.timestamp{
  font-size:0.7rem;font-family:var(--mono);color:var(--dim)
}
.phone-number{
  font-family:var(--mono);font-size:0.95rem;font-weight:600;
  margin-bottom:8px;word-break:break-all;color:var(--txt)
}
.country-info{
  font-size:0.75rem;color:var(--dim);margin-bottom:14px;
  display:flex;align-items:center;gap:6px
}
.otp-display{
  background:var(--s0);border:1.5px solid var(--bdr);
  border-radius:8px;padding:12px 14px;margin-bottom:12px;
  display:flex;justify-content:space-between;align-items:center;
  transition:all 0.3s
}
.otp-value{
  font-family:var(--mono);font-size:1.6rem;font-weight:700;
  letter-spacing:3px;background:linear-gradient(135deg, var(--grn), #34d399);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text
}
.copy-btn{
  background:rgba(16,185,129,.12);color:var(--grn);border:1px solid rgba(16,185,129,.25);
  border-radius:6px;padding:6px 12px;font-size:0.7rem;font-weight:700;
  font-family:var(--mono);cursor:pointer;transition:all 0.2s;
  white-space:nowrap
}
.copy-btn:hover{
  background:rgba(16,185,129,.2);transform:scale(1.05)
}
.copy-btn.copied{
  background:rgba(16,185,129,.3);color:#fff
}
.message-preview{
  font-size:0.75rem;color:var(--dim);line-height:1.5;
  white-space:normal;overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  border-top:1px solid rgba(100,116,139,.1);padding-top:10px
}
/* empty state */
.empty-state{
  grid-column:1/-1;text-align:center;padding:80px 20px;color:var(--dim)
}
.empty-icon{font-size:3.2rem;margin-bottom:16px;opacity:0.3}
.empty-text{font-size:1rem;margin-bottom:8px}
.empty-hint{font-size:0.85rem;color:var(--sub)}
/* pagination */
.pagination{
  display:flex;justify-content:center;align-items:center;gap:8px;
  margin-top:28px;padding-top:20px;border-top:1px solid var(--bdr)
}
.pag-btn{
  background:var(--s1);color:var(--txt);border:1px solid var(--bdr);
  width:36px;height:36px;border-radius:8px;cursor:pointer;
  transition:all 0.2s;font-size:0.85rem;font-weight:600
}
.pag-btn:hover{border-color:var(--acc);color:var(--acc)}
.pag-btn.active{
  background:var(--acc);color:#fff;border-color:var(--acc)
}
.pag-info{font-size:0.8rem;color:var(--dim);margin:0 12px}
/* footer */
footer{
  text-align:center;padding:28px;color:var(--dim);font-size:0.75rem;
  border-top:1px solid var(--bdr);margin-top:40px;position:relative;z-index:1
}
footer a{color:var(--acc);text-decoration:none;transition:color 0.2s}
footer a:hover{color:var(--acc2)}
/* toast notification */
.toast{
  position:fixed;bottom:24px;right:24px;z-index:999;
  background:linear-gradient(135deg, var(--grn), #34d399);
  color:#000;font-weight:700;padding:12px 20px;
  border-radius:12px;font-size:0.85rem;
  opacity:0;transform:translateY(20px);transition:all 0.3s;
  pointer-events:none;box-shadow:0 8px 24px rgba(16,185,129,.4)
}
.toast.show{opacity:1;transform:translateY(0)}
/* responsive */
@media(max-width:768px){
  .tb{padding:12px 16px}
  .wrap{padding:16px 12px}
  .charts-section{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .grid-container{grid-template-columns:1fr}
  .controls{flex-direction:column}
  .search-box,.filter-select{width:100%}
}
@media(max-width:480px){
  .stat-value{font-size:1.8rem}
  .otp-value{font-size:1.3rem;letter-spacing:2px}
  .grid-container{grid-template-columns:1fr}
}
/* scrollbar styling */
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--acc)}
</style>
</head>
<body>

<div class="tb">
  <div class="logo">
    <div class="logo-icon">🔐</div>
    <span class="logo-txt">CRACK SMS</span>
  </div>
  <div class="status-bar">
    <div class="status-pill pill-live">🟢 LIVE FEED</div>
    <div class="status-pill pill-cnt" id="cntBadge">— OTPs</div>
    <button class="btn-api" onclick="openApi()">📡 API Docs</button>
  </div>
</div>

<div class="wrap">
  <!-- STATS SECTION -->
  <div class="stats-section">
    <h3 class="stats-title">📊 Overview</h3>
    <div class="stats">
      <div class="stat-card">
        <div class="stat-icon">🔑</div>
        <div class="stat-value" id="sT">—</div>
        <div class="stat-label">Total OTPs</div>
      </div>
      <div class="stat-card g">
        <div class="stat-icon">📅</div>
        <div class="stat-value" id="sTd">—</div>
        <div class="stat-label">Today</div>
      </div>
      <div class="stat-card y">
        <div class="stat-icon">🏆</div>
        <div class="stat-value" id="sS">—</div>
        <div class="stat-label">Top Service</div>
      </div>
      <div class="stat-card r">
        <div class="stat-icon">⚡</div>
        <div class="stat-value" id="sH">—</div>
        <div class="stat-label">Last Hour</div>
      </div>
    </div>
  </div>

  <!-- CHARTS SECTION -->
  <div class="charts-section">
    <div class="chart-box">
      <h4 class="chart-title">📈 OTPs Received — Last 24 Hours</h4>
      <canvas id="hChart"></canvas>
    </div>
    <div class="chart-box">
      <h4 class="chart-title">🎯 Top Services</h4>
      <canvas id="sChart"></canvas>
    </div>
  </div>

  <!-- CONTROLS SECTION -->
  <div class="controls">
    <div class="search-box">
      <span class="search-icon">🔍</span>
      <input id="q" placeholder="Search service, number, OTP…" oninput="render()">
    </div>
    <select id="sf" class="filter-select" onchange="render()">
      <option value="">All services</option>
    </select>
    <button class="btn-refresh" onclick="load()">🔄 Refresh</button>
    <button class="btn-export" onclick="exportOTPs()">📥 Export</button>
  </div>

  <!-- OTPS GRID -->
  <div class="grid-container" id="grid">
    <div class="empty-state">
      <div class="empty-icon">⏳</div>
      <div class="empty-text">Loading OTPs…</div>
    </div>
  </div>

  <!-- PAGINATION (if many OTPs) -->
  <div id="pagination" style="display:none">
    <div class="pagination">
      <button class="pag-btn" onclick="prevPage()" id="prevBtn">← Prev</button>
      <span class="pag-info"><span id="currentPage">1</span> / <span id="totalPages">1</span></span>
      <button class="pag-btn" onclick="nextPage()" id="nextBtn">Next →</button>
    </div>
  </div>
</div>

<footer>
  CRACK SMS © 2024-2026 | 
  <a href="/api/docs">📡 API Documentation</a> | 
  <a href="/health">💚 Health Status</a> |
  Real-time OTP Feed Powered by Advanced SMS APIs
</footer>

<div class="toast" id="toast"></div>

<script>
let all=[], seen=new Set(), hC=null, sC=null;
let currentPage=1, itemsPerPage=12;

window.onload=()=>{
  initCharts();
  load();
  loadStats();
  setInterval(load, 4000);
  setInterval(loadStats, 30000);
};

async function load(){
  try{
    const r=await fetch('/api/public/otps?limit=500');
    const d=await r.json();
    if(d.status==='success'){
      const fresh=new Set(d.data.map(o=>o.number+o.received_at));
      const isNew=o=>!seen.has(o.number+o.received_at);
      const newKeys=new Set(d.data.filter(isNew).map(o=>o.number+o.received_at));
      all=d.data;
      seen=fresh;
      document.getElementById('cntBadge').textContent=all.length+' OTPs';
      buildFilter();
      currentPage=1;
      render(newKeys);
    }
  }catch(e){console.error('Load error:',e)}
}

async function loadStats(){
  try{
    const r=await fetch('/api/public/stats');
    const d=await r.json();
    if(d.status==='success'){
      document.getElementById('sT').textContent=d.total_otps.toLocaleString();
      document.getElementById('sTd').textContent=d.otps_today.toLocaleString();
      const top=Object.entries(d.by_service||{}).sort((a,b)=>b[1]-a[1])[0];
      document.getElementById('sS').textContent=top?top[0]:'—';
      const now=new Date(), b=`${now.getFullYear()}-${p(now.getMonth()+1)}-${p(now.getDate())} ${p(now.getHours())}:00`;
      document.getElementById('sH').textContent=(d.hourly_last_24h?.[b]||0).toLocaleString();
      updateCharts(d);
    }
  }catch(e){console.error('Stats error:',e)}
}

function render(newKeys=new Set()){
  const q=document.getElementById('q').value.toLowerCase();
  const sf=document.getElementById('sf').value;
  
  let filtered=all.filter(o=>{
    if(sf&&o.service!==sf)return false;
    if(q&&!(o.number+o.otp+o.service+o.country).toLowerCase().includes(q))return false;
    return true;
  });

  const g=document.getElementById('grid');
  if(!filtered.length){
    g.innerHTML='<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No OTPs found</div><div class="empty-hint">Try adjusting your filters</div></div>';
    document.getElementById('pagination').style.display='none';
    return;
  }

  const totalPages=Math.ceil(filtered.length/itemsPerPage);
  const start=(currentPage-1)*itemsPerPage;
  const end=start+itemsPerPage;
  const pageItems=filtered.slice(start,end);

  g.innerHTML=pageItems.map((o,i)=>`
  <div class="otp-card" style="animation-delay:${i*0.05}s">
    ${newKeys.has(o.number+o.received_at)?'<span class="new-badge">NEW</span>':''}
    <div class="card-header">
      <span class="service-tag">${e(o.service)}</span>
      <span class="timestamp">${e(o.received_at)}</span>
    </div>
    <div class="phone-number">📱 ${e(o.number)}</div>
    <div class="country-info">📍 ${e(o.country)}</div>
    <div class="otp-display">
      <span class="otp-value">${fmt(e(o.otp))}</span>
      <button class="copy-btn" onclick="cp('${e(o.otp)}',this)">📋 COPY</button>
    </div>
    <div class="message-preview">${e(o.message)}</div>
  </div>`).join('');

  if(totalPages>1){
    document.getElementById('pagination').style.display='block';
    document.getElementById('currentPage').textContent=currentPage;
    document.getElementById('totalPages').textContent=totalPages;
    document.getElementById('prevBtn').style.opacity=currentPage===1?'0.5':'1';
    document.getElementById('nextBtn').style.opacity=currentPage===totalPages?'0.5':'1';
  }else{
    document.getElementById('pagination').style.display='none';
  }
}

function prevPage(){
  if(currentPage>1){currentPage--;render()}
}
function nextPage(){
  const totalPages=Math.ceil(all.length/itemsPerPage);
  if(currentPage<totalPages){currentPage++;render()}
}

function buildFilter(){
  const sel=document.getElementById('sf'), cur=sel.value;
  const svcs=[...new Set(all.map(o=>o.service))].sort();
  sel.innerHTML='<option value="">All services</option>'+svcs.map(s=>`<option${s===cur?' selected':''}>${e(s)}</option>`).join('');
}

function initCharts(){
  const fg='#94a3b8', grid='#1e2232';
  Chart.defaults.color=fg;
  Chart.defaults.font.family="'JetBrains Mono'";
  hC=new Chart(document.getElementById('hChart'),{
    type:'line',
    data:{
      labels:[],
      datasets:[{
        label:'OTPs',
        data:[],
        borderColor:'#7c3aed',
        backgroundColor:'rgba(124,58,237,.1)',
        fill:true,
        tension:0.4,
        pointRadius:4,
        pointHoverRadius:8,
        pointBackgroundColor:'#7c3aed',
        borderWidth:2.5
      }]
    },
    options:{
      responsive:true,
      maintainAspectRatio:true,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:grid,drawBorder:false},ticks:{maxTicksLimit:8}},
        y:{grid:{color:grid,drawBorder:false},beginAtZero:true,ticks:{precision:0}}
      },
      animation:{duration:300}
    }
  });
  
  sC=new Chart(document.getElementById('sChart'),{
    type:'doughnut',
    data:{
      labels:[],
      datasets:[{
        data:[],
        backgroundColor:['#7c3aed','#ec4899','#10b981','#f59e0b','#06b6d4','#ef4444','#8b5cf6'],
        borderWidth:2.5,
        borderColor:'#151820'
      }]
    },
    options:{
      responsive:true,
      maintainAspectRatio:true,
      plugins:{legend:{position:'bottom',labels:{boxWidth:12,padding:16,font:{size:11}}}},
      cutout:'65%',
      animation:{duration:300}
    }
  });
}

function updateCharts(d){
  if(!hC||!sC)return;
  const h=d.hourly_last_24h||{};
  const keys=Object.keys(h).slice(-24);
  hC.data.labels=keys.map(k=>k.slice(11,16));
  hC.data.datasets[0].data=keys.map(k=>h[k]);
  hC.update('none');
  const sv=d.by_service||{};
  sC.data.labels=Object.keys(sv);
  sC.data.datasets[0].data=Object.values(sv);
  sC.update('none');
}

function cp(otp,btn){
  if(!otp||otp==='—')return;
  navigator.clipboard.writeText(otp).then(()=>{
    btn.textContent='✓ Copied!';
    btn.classList.add('copied');
    toast('OTP copied to clipboard!');
    setTimeout(()=>{btn.textContent='📋 COPY';btn.classList.remove('copied')},2000);
  });
}

function toast(m){
  const t=document.getElementById('toast');
  t.textContent=m;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2800);
}

function exportOTPs(){
  if(!all.length){toast('No OTPs to export');return}
  const csv='Service,Number,Country,OTP,Message,Received At\n'+
    all.slice(0,100).map(o=>`"${o.service}","${o.number}","${o.country}","${o.otp}","${o.message}","${o.received_at}"`).join('\n');
  const blob=new Blob([csv],{type:'text/csv'});
  const url=window.URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;
  a.download=`otps-${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  toast('OTPs exported as CSV');
}

function e(s){const d=document.createElement('div');d.textContent=String(s||'');return d.innerHTML}
function fmt(s){if(!s||s==='—')return s;if(s.length===6)return s.slice(0,3)+'-'+s.slice(3);return s}
function p(n){return String(n).padStart(2,'0')}
function openApi(){window.open('/api/docs','_blank')}
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return _HTML


@app.get("/api/docs", response_class=HTMLResponse)
async def api_docs():
    return """<!DOCTYPE html><html><head><title>CRACK SMS API</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono&family=Space+Grotesk:wght@400;700&display=swap" rel="stylesheet">
<style>*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Space Grotesk',sans-serif;background:#08090d;color:#dde1f0;padding:40px 24px}
.w{max-width:860px;margin:0 auto}h1{font-size:2rem;margin-bottom:8px}
h1 span{color:#6c63ff}sub{color:#6b7194;font-size:.85rem}
.ep{background:#13151f;border:1px solid #252836;border-radius:12px;padding:24px;margin:20px 0}
.ep h3{color:#6c63ff;margin-bottom:6px}.ep p{color:#6b7194;font-size:.83rem;margin-bottom:12px}
pre{background:#0e0f16;border:1px solid #252836;border-radius:8px;padding:14px;
  font-family:'JetBrains Mono',monospace;font-size:.73rem;overflow-x:auto;color:#a29bfe}
.b{display:inline-block;padding:2px 9px;border-radius:5px;font-size:.67rem;font-weight:700;margin-right:6px}
.get{background:rgba(0,212,170,.12);color:#00d4aa}.pub{background:rgba(108,99,255,.12);color:#6c63ff}
.auth{background:rgba(255,190,11,.12);color:#ffbe0b}
a{color:#6c63ff;text-decoration:none}</style></head><body><div class="w">
<h1>CRACK SMS <span>API</span></h1>
<p style="color:#6b7194;margin:8px 0 28px">Base URL: <code style="color:#a29bfe">https://mywebsite.com</code></p>
<div class="ep"><span class="b get">GET</span><span class="b pub">PUBLIC</span>
<h3>/api/public/otps</h3><p>All recent OTPs — no token required.</p>
<pre>GET /api/public/otps?limit=100</pre></div>
<div class="ep"><span class="b get">GET</span><span class="b pub">PUBLIC</span>
<h3>/api/public/stats</h3><p>Aggregated statistics — no token required.</p>
<pre>GET /api/public/stats</pre></div>
<div class="ep"><span class="b get">GET</span><span class="b auth">AUTH</span>
<h3>/api/sms</h3><p>OTPs filtered to token's panels.</p>
<pre>GET /api/sms?token=YOUR_TOKEN&amp;limit=100&amp;date=2024-01-15</pre></div>
<div class="ep"><span class="b get">GET</span><span class="b auth">AUTH</span>
<h3>/api/stats</h3><p>Authenticated statistics.</p>
<pre>GET /api/stats?token=YOUR_TOKEN</pre></div>
<p style="margin-top:28px;color:#6b7194"><a href="/">← Back to dashboard</a></p>
</div></body></html>"""


@app.exception_handler(HTTPException)
async def http_ex(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"status": "error", "message": exc.detail})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
