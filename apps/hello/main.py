from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/hello")
def hello():
    return {"message": "Yoo its me from the cluster you fuck"}

@app.get("/test_config")
def test_config():
    return {"message": f"This is from the ConfigMap: {os.getenv('GREETING')}"}