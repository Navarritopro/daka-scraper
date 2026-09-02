from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import asyncpg
import os
import httpx
from pydantic import BaseModel

app = FastAPI()

DB_URL = os.getenv("POSTGRES_URL")
GITHUB_TOKEN = os.getenv("GH_PAT")
REPO = os.getenv("GITHUB_REPO")

# ================= ENDPOINTS =================

@app.get("/api/kpis")
async def get_kpis():
    if not DB_URL:
        raise HTTPException(500, "POSTGRES_URL missing")
    conn = await asyncpg.connect(DB_URL)
    try:
        total = await conn.fetchval("SELECT COUNT(*) FROM products")
        # Cambios en últimas 24h
        changes = await conn.fetchval("""
            SELECT COUNT(*) FROM price_history
            WHERE scraped_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'
        """)
        # Sin stock (opcional, si no guardas stock, pones 0)
        out_of_stock = await conn.fetchval("SELECT COUNT(*) FROM products WHERE sap NOT IN (SELECT sap FROM price_history WHERE scraped_at > CURRENT_TIMESTAMP - INTERVAL '1 day'))") or 0
        # Promociones (placeholder)
        return {
            "total_products": total,
            "price_changes": changes,
            "out_of_stock": out_of_stock,
            "promotions": 0
        }
    finally:
        await conn.close()

@app.get("/api/products")
async def get_products(limit: int = 50, search: str = None):
    if not DB_URL:
        raise HTTPException(500, "POSTGRES_URL missing")
    conn = await asyncpg.connect(DB_URL)
    try:
        query = """
            SELECT DISTINCT ON (p.sap)
                p.sap, p.nombre, h.price_usd, h.scraped_at as last_update
            FROM products p
            JOIN price_history h ON p.sap = h.sap
        """
        params = []
        if search:
            query += " WHERE p.nombre ILIKE $1 OR p.sap ILIKE $1"
            params.append(f"%{search}%")
        query += " ORDER BY p.sap, h.scraped_at DESC LIMIT $2"
        params.append(limit)
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()

class TriggerResponse(BaseModel):
    message: str
    workflow_id: str = "scrape.yml"

@app.post("/api/trigger", response_model=TriggerResponse)
async def trigger_scrape():
    if not GITHUB_TOKEN or not REPO:
        raise HTTPException(500, "GitHub config missing")
    async with httpx.AsyncClient() as client:
        url = f"https://api.github.com/repos/{REPO}/actions/workflows/scrape.yml/dispatches"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        payload = {"ref": "main"}
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 204:
            raise HTTPException(500, f"GitHub API error: {resp.text}")
        return {"message": "Scraping iniciado manualmente", "workflow_id": "scrape.yml"}

# Endpoint de salud para Vercel
@app.get("/")
async def root():
    return {"status": "ok", "service": "daka-scraper-api"}