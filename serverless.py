from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import sqlite3
import json
import subprocess
import docker


class Function(BaseModel):
    name: str
    language: str
    code: str
    runtime: str
    timeout: Optional[int] = 5


app = FastAPI()
client = docker.from_env()
db_path = "functions.db"


def init_db():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS functions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        language TEXT,
        code TEXT,
        runtime TEXT,
        timeout INTEGER
    )"""
    )
    conn.commit()
    conn.close()


init_db()


def build_docker_image(language):
    if language == "py":
        return """
FROM python:3.9-slim
WORKDIR /app
COPY ./script.py /app
CMD ["python", "/app/script.py"]"""
    elif language == "js":
        return """
FROM node:18-slim
WORKDIR /app
COPY ./script.js /app
CMD ["node", "/app/script.js"]"""
    else:
        raise ValueError("Unsupported language")

templates = Jinja2Templates(directory="templates")

def build_nanos_image(language):
    if language == "py":
        return "eyberg/python:3.10.6", {"Args": ["script.py"], "Files": ["script.py"]}
    elif language == "js":
        return "eyberg/node:20.5.0", {"Args": ["script.js"], "Files": ["script.js"]}
    else:
        raise ValueError("Unsupported language")

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/functions")
def create_function(data: Function):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO functions (name, language, code, runtime, timeout) VALUES (?, ?, ?, ?, ?)",
        (data.name, data.language, data.code, data.runtime, data.timeout),
    )
    script_path = f"/tmp/docker/script.{data.language}"
    with open(script_path, "w") as f:
        f.write(data.code)

    if data.runtime == "docker":
        with open("/tmp/docker/Dockerfile", "w") as f:
            f.write(build_docker_image(data.language))

        image_tag = f"function-{data.name}:{cursor.lastrowid}"
        client.images.build(path="/tmp/docker", tag=image_tag)
    elif data.runtime == "nanos":
        with open("/tmp/docker/config.json", "w") as f:
            package, config = build_nanos_image(data.language)
            f.write(json.dumps(config))
        image_name = f"function-{data.name}-{cursor.lastrowid}"
        subprocess.run(
            [
                "ops",
                "build",
                "-t",
                image_name,
                "--package",
                package,
                "-c",
                "config.json",
                script_path,
            ],
            cwd="/tmp/docker",
            check=True,
        )
    else:
        return HTTPException(400, detail="Unsupported Runtime")

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

@app.get("/all-functions")
def get_all_functions():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM functions")
    result = cursor.fetchall()
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Function not found")
    return list(map(lambda result: dict(zip(["id", "name", "language", "code", "runtime", "timeout"], result)), result))


@app.delete("/functions/{function_id}")
def delete_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM functions WHERE id = ?", (function_id,))
    conn.commit()
    conn.close()
    return {"message": "Function deleted"}


def run_docker(name, timeout, id):
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


def run_nanos(timeout, language):
    package = "eyberg/python:3.10.6" if language == "py" else "eyberg/node:20.5.0"
    try:
        proc = subprocess.run(
            ["ops", "pkg", "load", package, "-c", "config.json"],
            capture_output=True,
            cwd="/tmp/docker",
            timeout=timeout,
        )
        output = proc.stdout.decode()
        return {"output": output}
    except Exception as e:
        return {"error": str(e)}


@app.post("/execute/{function_id}")
def execute_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM functions WHERE id = ?", (function_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Function not found")

    id, name, language, _, runtime, timeout = result

    if runtime == "docker":
        return run_docker(name, timeout, id)
    elif runtime == "nanos":
        return run_nanos(timeout, language)
