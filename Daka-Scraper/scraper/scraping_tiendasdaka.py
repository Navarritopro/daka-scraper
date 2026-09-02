import re
import asyncio
import os
import time
import asyncpg
import requests
import smtplib
from email.mime.text import MIMEText
from urllib.parse import unquote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= CONFIGURACIÓN =================
BASE_URL = "https://tiendasdaka.com/ve/store"
MAX_PAGES = 200
MAX_RETRIES_PAGINA = 3
MAX_PAGINAS_VACIAS_CONSECUTIVAS = 3
RETRIES_POR_PAGINA_VACIA = 2
DELAY = 1
ALERT_THRESHOLD_PERCENT = float(os.getenv("ALERT_THRESHOLD", 5))  # 5% por defecto
# =================================================

def extraer_sap_de_url_imagen(url_imagen):
    if not url_imagen:
        return None
    url_decoded = unquote(url_imagen)
    match = re.search(r"(?:LH|LM|LB|LD|LJ|LC|LF|LL|LT)-\d+", url_decoded)
    return match.group(0) if match else None

def obtener_productos_de_pagina(page):
    return page.evaluate("""
        () => {
            const wrappers = document.querySelectorAll('[data-testid="product-wrapper"]');
            return Array.from(wrappers).map(wrapper => {
                const nombreTag = wrapper.querySelector('span.line-clamp-2');
                const nombre = nombreTag ? nombreTag.innerText.trim() : null;
                const img = wrapper.querySelector('img');
                const src = img ? (img.getAttribute('src') || img.getAttribute('srcset') || '') : '';
                const linkTag = wrapper.querySelector('a[href]');
                const link = linkTag ? linkTag.getAttribute('href') : '';
                const precioTag = wrapper.querySelector('[data-testid="price"]');
                const precio_usd = precioTag ? precioTag.innerText.trim() : '';
                return { nombre, src, link, precio_usd };
            }).filter(p => p.nombre);
        }
    """)

def cargar_pagina(page, url, max_reintentos=MAX_RETRIES_PAGINA):
    for intento in range(1, max_reintentos + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector('[data-testid="product-wrapper"]', timeout=10000)
            page.wait_for_timeout(1000)
            return True
        except (PlaywrightTimeoutError, Exception) as e:
            print(f"   ⚠️ Intento {intento} fallido: {e}")
            if intento < max_reintentos:
                time.sleep(2 * intento)
    return False

def extraer_todos_los_productos():
    """Función sincrónica que ejecuta el scraping y devuelve lista de dicts."""
    productos_total = []
    sin_sap = 0
    paginas_fallidas = []
    paginas_vacias = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        vacias_consecutivas = 0

        for current_page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}?page={current_page}" if current_page > 1 else BASE_URL
            print(f"\n📄 Página {current_page}: {url}")

            if not cargar_pagina(page, url):
                paginas_fallidas.append(current_page)
                vacias_consecutivas += 1
                if vacias_consecutivas >= MAX_PAGINAS_VACIAS_CONSECUTIVAS:
                    print(f"🛑 {MAX_PAGINAS_VACIAS_CONSECUTIVAS} fallos consecutivos. Deteniendo.")
                    break
                time.sleep(DELAY)
                continue

            productos_pagina = obtener_productos_de_pagina(page)

            for intento in range(RETRIES_POR_PAGINA_VACIA):
                if not productos_pagina:
                    time.sleep(2)
                    if not cargar_pagina(page, url, max_reintentos=1):
                        break
                    productos_pagina = obtener_productos_de_pagina(page)
                else:
                    break

            if not productos_pagina:
                paginas_vacias.append(current_page)
                vacias_consecutivas += 1
                if vacias_consecutivas >= MAX_PAGINAS_VACIAS_CONSECUTIVAS:
                    print(f"🛑 {MAX_PAGINAS_VACIAS_CONSECUTIVAS} páginas vacías. Asumiendo fin.")
                    break
                time.sleep(DELAY)
                continue

            vacias_consecutivas = 0

            for p in productos_pagina:
                sap = extraer_sap_de_url_imagen(p["src"])
                nombre = p["nombre"]
                url_producto = p["link"]
                precio = p["precio_usd"]
                # Limpiar precio (quitar símbolos, convertir a float)
                if precio:
                    precio_clean = re.sub(r'[^\d.]', '', precio)
                    try:
                        precio_float = float(precio_clean)
                    except:
                        precio_float = None
                else:
                    precio_float = None

                item = {
                    "sap": sap or "PENDIENTE",
                    "nombre": nombre,
                    "precio_usd": precio_float,
                    "url_producto": url_producto
                }
                productos_total.append(item)
                if not sap:
                    sin_sap += 1

            print(f"   → {len(productos_pagina)} productos en página (total acumulado: {len(productos_total)})")

            time.sleep(DELAY)

        # Segunda pasada para fallidas/vacías
        paginas_a_reintentar = set(paginas_fallidas + paginas_vacias)
        if paginas_a_reintentar:
            print(f"\n🔁 Reintentando {len(paginas_a_reintentar)} páginas problemáticas...")
            for num_pagina in sorted(paginas_a_reintentar):
                url = f"{BASE_URL}?page={num_pagina}" if num_pagina > 1 else BASE_URL
                if not cargar_pagina(page, url, max_reintentos=2):
                    continue
                productos_pagina = obtener_productos_de_pagina(page)
                if productos_pagina:
                    for p in productos_pagina:
                        sap = extraer_sap_de_url_imagen(p["src"])
                        precio_clean = re.sub(r'[^\d.]', '', p["precio_usd"]) if p["precio_usd"] else None
                        precio_float = float(precio_clean) if precio_clean else None
                        productos_total.append({
                            "sap": sap or "PENDIENTE",
                            "nombre": p["nombre"],
                            "precio_usd": precio_float,
                            "url_producto": p["link"]
                        })
                        if not sap:
                            sin_sap += 1
                time.sleep(DELAY)

        browser.close()

    return productos_total, sin_sap

# ================= FUNCIONES ASYNC DE BD Y ALERTAS =================

async def guardar_en_bd(productos, job_id, db_url):
    conn = await asyncpg.connect(db_url)
    try:
        for p in productos:
            sap = p["sap"]
            if sap == "PENDIENTE":
                continue  # no guardamos productos sin SAP
            nombre = p["nombre"]
            url = p["url_producto"]
            precio = p["precio_usd"]

            await conn.execute("""
                INSERT INTO products (sap, nombre, url_producto)
                VALUES ($1, $2, $3)
                ON CONFLICT (sap) DO UPDATE SET
                    nombre = EXCLUDED.nombre,
                    url_producto = EXCLUDED.url_producto
            """, sap, nombre, url)

            await conn.execute("""
                INSERT INTO price_history (sap, price_usd, scraped_at, job_id)
                VALUES ($1, $2, CURRENT_TIMESTAMP, $3)
            """, sap, precio, job_id)

        await conn.execute("""
            UPDATE scraping_jobs
            SET status = 'success', finished_at = CURRENT_TIMESTAMP, products_found = $1
            WHERE id = $2
        """, len(productos), job_id)
    except Exception as e:
        await conn.execute("""
            UPDATE scraping_jobs
            SET status = 'failed', error_message = $1, finished_at = CURRENT_TIMESTAMP
            WHERE id = $2
        """, str(e), job_id)
        raise e
    finally:
        await conn.close()

async def verificar_alertas(job_id, db_url):
    conn = await asyncpg.connect(db_url)
    rows = await conn.fetch("""
        WITH current_job AS (
            SELECT sap, price_usd FROM price_history WHERE job_id = $1
        ),
        previous_job AS (
            SELECT sap, price_usd FROM price_history
            WHERE job_id = (SELECT MAX(job_id) FROM price_history WHERE job_id < $1)
        )
        SELECT
            c.sap,
            c.price_usd AS current_price,
            p.price_usd AS old_price,
            ((c.price_usd - p.price_usd) / NULLIF(p.price_usd, 0)) * 100 AS change_pct
        FROM current_job c
        JOIN previous_job p ON c.sap = p.sap
        WHERE ABS(((c.price_usd - p.price_usd) / NULLIF(p.price_usd, 0)) * 100) > $2
    """, job_id, ALERT_THRESHOLD_PERCENT)
    await conn.close()

    if not rows:
        return

    mensaje = "🔔 ALERTAS DE PRECIOS DAKA 🔔\n\n"
    for r in rows:
        mensaje += f"📦 {r['sap']}: ${r['old_price']:.2f} → ${r['current_price']:.2f} ({r['change_pct']:.1f}%)\n"

    # --- Enviar por Telegram ---
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": mensaje}, timeout=10)
        except Exception as e:
            print(f"Error enviando Telegram: {e}")

    # --- Enviar por Email ---
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASS")
    if email_user and email_pass:
        try:
            msg = MIMEText(mensaje)
            msg['Subject'] = 'Alertas Daka - Cambios de precio'
            msg['From'] = email_user
            msg['To'] = email_user
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(email_user, email_pass)
                server.sendmail(email_user, [email_user], msg.as_string())
        except Exception as e:
            print(f"Error enviando Email: {e}")

async def run_scrape():
    DB_URL = os.getenv("POSTGRES_URL")
    if not DB_URL:
        raise Exception("POSTGRES_URL no configurada")

    # 1. Registrar inicio del job
    conn = await asyncpg.connect(DB_URL)
    job_id = await conn.fetchval(
        "INSERT INTO scraping_jobs (status, started_at) VALUES ('running', CURRENT_TIMESTAMP) RETURNING id"
    )
    await conn.close()

    # 2. Ejecutar scraping (sincrónico, lo ejecutamos en hilo)
    loop = asyncio.get_event_loop()
    productos, sin_sap = await loop.run_in_executor(None, extraer_todos_los_productos)

    # 3. Guardar en BD
    await guardar_en_bd(productos, job_id, DB_URL)

    # 4. Verificar alertas
    await verificar_alertas(job_id, DB_URL)

    print(f"✅ Scraping finalizado. Job ID: {job_id}, Productos: {len(productos)}, Sin SAP: {sin_sap}")

if __name__ == "__main__":
    asyncio.run(run_scrape())