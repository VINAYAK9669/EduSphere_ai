from fastapi import FastAPI

app = FastAPI(
    title="EduSphere AI",
    description="AI-powered education platform",
    version="1.0.0"
)

@app.get("/")
def root():
    return {"status": "EduSphere AI is running 🚀"}