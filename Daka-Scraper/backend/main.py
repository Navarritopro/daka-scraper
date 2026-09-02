from fastapi import FastAPI

app = FastAPI()

@app.get("/api/kpis")
async def kpis():
    return {"total_products": 0, "price_changes": 0, "out_of_stock": 0, "promotions": 0}

@app.get("/")
async def root():
    return {"status": "ok", "message": "API funcionando"}
