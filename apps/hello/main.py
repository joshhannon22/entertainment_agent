from fastapi import FastAPI

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/hello")
def hello():
    return {"message": "Yoo its me from the cluster you fuck"}