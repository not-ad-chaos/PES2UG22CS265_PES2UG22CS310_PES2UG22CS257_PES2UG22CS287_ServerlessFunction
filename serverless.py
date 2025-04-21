from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import sqlite3
import docker

class Function(BaseModel):
    name: str
    language: str
    code: str
    timeout: Optional[int] = 5

app = FastAPI()
client = docker.from_env()
db_path = 'functions.db'

def init_db():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS functions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        language TEXT,
        code TEXT,
        timeout INTEGER
    )''')
    conn.commit()
    conn.close()

init_db()

def build_image(language):
    if language == 'py':
        dockerfile = """
FROM python:3.9-slim
WORKDIR /app
COPY ./script.py /app
CMD ["python", "/app/script.py"]"""
    elif language == 'js':
        dockerfile = """
FROM node:18-slim
WORKDIR /app
COPY ./script.js /app
CMD ["node", "/app/script.js"]"""
    else:
        raise ValueError("Unsupported language")
    return dockerfile

@app.post("/functions")
def create_function(data: Function):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO functions (name, language, code, timeout) VALUES (?, ?, ?, ?)",
                   (data.name, data.language, data.code, data.timeout))
    script_path = f"/tmp/script.{data.language}"

    with open(script_path, "w") as f:
        f.write(data.code)

    dockerfile = build_image(data.language)
    with open("/tmp/Dockerfile", "w") as f:
        f.write(dockerfile)

    image_tag = f"function-{data.name}:{cursor.lastrowid}"
    client.images.build(path="/tmp", tag=image_tag)
    conn.commit()
    conn.close()
    return {"id": cursor.lastrowid, "message": "Function created"}

@app.get("/functions/{function_id}")
def get_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM functions WHERE id = ?", (function_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Function not found")
    return dict(zip(["id", "name", "language", "code", "timeout"], result))

@app.delete("/functions/{function_id}")
def delete_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM functions WHERE id = ?", (function_id,))
    conn.commit()
    conn.close()
    return {"message": "Function deleted"}

# Execution with Timeout Enforcement
@app.post("/execute/{function_id}")
def execute_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM functions WHERE id = ?", (function_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Function not found")

    id, name, _, _, timeout = result

    try:
        container = client.containers.run(
            image=f"function-{name}:{id}",
            detach=True,
        )
        container.wait(timeout=timeout)
        output = container.logs().decode()
        container.stop()
        container.remove()
        client.containers.prune()
        return {"output": output}
    except Exception as e:
        return {"error": str(e)}
