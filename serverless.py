from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, Dict, Any
import sqlite3
import json
import subprocess
import docker
import time
import psutil
import datetime


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
    # Create metrics table
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        function_id INTEGER,
        execution_time REAL,
        memory_usage REAL,
        cpu_usage REAL,
        status TEXT,
        timestamp TEXT,
        error_message TEXT,
        FOREIGN KEY (function_id) REFERENCES functions (id) ON DELETE CASCADE
    )"""
    )
    # Create execution count table for quick statistics
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS execution_stats (
        function_id INTEGER PRIMARY KEY,
        execution_count INTEGER DEFAULT 0,
        success_count INTEGER DEFAULT 0,
        failure_count INTEGER DEFAULT 0,
        avg_execution_time REAL DEFAULT 0,
        last_executed TEXT,
        FOREIGN KEY (function_id) REFERENCES functions (id) ON DELETE CASCADE
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
        return {"Args": ["script.py"], "Files": ["script.py"]}
    elif language == "js":
        return {"Args": ["script.js"], "Files": ["script.js"]}
    else:
        raise ValueError("Unsupported language")


def record_metrics(function_id: int, metrics_data: Dict[str, Any]):
    """Record metrics for a function execution."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Insert into metrics table
    cursor.execute(
        """INSERT INTO metrics 
        (function_id, execution_time, memory_usage, cpu_usage, status, timestamp, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            function_id,
            metrics_data["execution_time"],
            metrics_data["memory_usage"],
            metrics_data["cpu_usage"],
            metrics_data["status"],
            metrics_data["timestamp"],
            metrics_data.get("error_message", None),
        ),
    )

    # Update execution stats
    cursor.execute(
        "SELECT * FROM execution_stats WHERE function_id = ?", (function_id,)
    )
    stats = cursor.fetchone()

    if not stats:
        # Create new entry if not exists
        cursor.execute(
            """INSERT INTO execution_stats 
            (function_id, execution_count, success_count, failure_count, avg_execution_time, last_executed)
            VALUES (?, 1, ?, ?, ?, ?)""",
            (
                function_id,
                1 if metrics_data["status"] == "success" else 0,
                0 if metrics_data["status"] == "success" else 1,
                metrics_data["execution_time"],
                metrics_data["timestamp"],
            ),
        )
    else:
        # Update existing entry
        _, exec_count, success_count, failure_count, avg_time, _ = stats
        exec_count += 1
        if metrics_data["status"] == "success":
            success_count += 1
        else:
            failure_count += 1

        # Update average execution time
        avg_time = ((avg_time * (exec_count - 1)) + metrics_data["execution_time"]) / exec_count

        cursor.execute(
            """UPDATE execution_stats
            SET execution_count = ?, success_count = ?, failure_count = ?,
            avg_execution_time = ?, last_executed = ?
            WHERE function_id = ?""",
            (
                exec_count,
                success_count,
                failure_count,
                avg_time,
                metrics_data["timestamp"],
                function_id,
            ),
        )

    conn.commit()
    conn.close()


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
    function_id = cursor.lastrowid
    script_path = f"/tmp/docker/script.{data.language}"
    with open(script_path, "w") as f:
        f.write(data.code)

    if data.runtime == "docker":
        with open("/tmp/docker/Dockerfile", "w") as f:
            f.write(build_docker_image(data.language))

        image_tag = f"function-{data.name}:{function_id}"
        client.images.build(path="/tmp/docker", tag=image_tag)
    elif data.runtime == "nanos":
        with open("/tmp/docker/config.json", "w") as f:
            config = build_nanos_image(data.language)
            f.write(json.dumps(config))
    else:
        return HTTPException(400, detail="Unsupported Runtime")

    conn.commit()
    conn.close()
    return {"id": function_id, "message": "Function created"}


@app.get("/functions/{function_id}")
def get_function(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM functions WHERE id = ?", (function_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        raise HTTPException(status_code=404, detail="Function not found")
    return dict(zip(["id", "name", "language", "code", "runtime", "timeout"], result))


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


def run_docker(name, timeout, function_id):
    start_time = time.time()
    start_cpu = psutil.cpu_percent(interval=0.1)
    start_memory = psutil.virtual_memory().percent
    metrics = {
        "timestamp": datetime.datetime.now().isoformat(),
        "status": "success",
    }

    try:
        container = client.containers.run(
            image=f"function-{name}:{function_id}",
            detach=True,
        )
        container.wait(timeout=timeout)
        output = container.logs().decode()
        container.stop()
        container.remove()
        client.containers.prune()

        # Calculate metrics
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=0.1)
        end_memory = psutil.virtual_memory().percent

        metrics.update({
            "execution_time": end_time - start_time,
            "cpu_usage": end_cpu - start_cpu,
            "memory_usage": end_memory - start_memory,
        })

        record_metrics(function_id, metrics)
        return {"output": output}
    except Exception as e:
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=0.1)
        end_memory = psutil.virtual_memory().percent

        metrics.update({
            "execution_time": end_time - start_time,
            "cpu_usage": end_cpu - start_cpu,
            "memory_usage": end_memory - start_memory,
            "status": "failure",
            "error_message": str(e),
        })

        record_metrics(function_id, metrics)
        return {"error": str(e)}


def run_nanos(timeout, language, function_id):
    start_time = time.time()
    start_cpu = psutil.cpu_percent(interval=0.1)
    start_memory = psutil.virtual_memory().percent
    metrics = {
        "timestamp": datetime.datetime.now().isoformat(),
        "status": "success",
    }

    package = "eyberg/python:3.10.6" if language == "py" else "eyberg/node:20.5.0"
    try:
        proc = subprocess.run(
            ["ops", "pkg", "load", package, "-c", "config.json"],
            capture_output=True,
            cwd="/tmp/docker",
            timeout=timeout,
        )
        output = proc.stdout.decode()

        # Calculate metrics
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=0.1)
        end_memory = psutil.virtual_memory().percent

        metrics.update({
            "execution_time": end_time - start_time,
            "cpu_usage": end_cpu - start_cpu,
            "memory_usage": end_memory - start_memory,
        })

        record_metrics(function_id, metrics)
        return {"output": output}
    except Exception as e:
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=0.1)
        end_memory = psutil.virtual_memory().percent

        metrics.update({
            "execution_time": end_time - start_time,
            "cpu_usage": end_cpu - start_cpu,
            "memory_usage": end_memory - start_memory,
            "status": "failure",
            "error_message": str(e),
        })

        record_metrics(function_id, metrics)
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
        return run_nanos(timeout, language, id)

# New endpoints for metrics

@app.get("/metrics/{function_id}")
def get_function_metrics(function_id: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get function info
    cursor.execute("SELECT name FROM functions WHERE id = ?", (function_id,))
    function = cursor.fetchone()
    if not function:
        conn.close()
        raise HTTPException(status_code=404, detail="Function not found")

    # Get execution stats
    cursor.execute("SELECT * FROM execution_stats WHERE function_id = ?", (function_id,))
    stats = cursor.fetchone()
    if not stats:
        stats_data = {
            "execution_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "avg_execution_time": 0,
            "last_executed": None,
        }
    else:
        stats_data = {
            "execution_count": stats[1],
            "success_count": stats[2],
            "failure_count": stats[3],
            "avg_execution_time": stats[4],
            "last_executed": stats[5],
        }

    # Get detailed metrics (last 10)
    cursor.execute(
        """SELECT execution_time, memory_usage, cpu_usage, status, timestamp, error_message
        FROM metrics WHERE function_id = ? ORDER BY timestamp DESC LIMIT 10""",
        (function_id,)
    )
    metrics = cursor.fetchall()
    metrics_data = []
    for metric in metrics:
        metrics_data.append({
            "execution_time": metric[0],
            "memory_usage": metric[1],
            "cpu_usage": metric[2],
            "status": metric[3],
            "timestamp": metric[4],
            "error_message": metric[5],
        })

    conn.close()

    return {
        "function_id": function_id,
        "function_name": function[0],
        "stats": stats_data,
        "recent_executions": metrics_data,
    }


@app.get("/metrics")
def get_all_metrics():
    """Get metrics summary for all functions."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Join functions with execution stats
    cursor.execute(
        """SELECT f.id, f.name, COALESCE(e.execution_count, 0) as count, 
        COALESCE(e.success_count, 0) as success, COALESCE(e.failure_count, 0) as failure,
        COALESCE(e.avg_execution_time, 0) as avg_time, e.last_executed
        FROM functions f
        LEFT JOIN execution_stats e ON f.id = e.function_id"""
    )
    results = cursor.fetchall()

    conn.close()

    metrics_data = []
    for result in results:
        metrics_data.append({
            "function_id": result[0],
            "function_name": result[1],
            "execution_count": result[2],
            "success_count": result[3],
            "failure_count": result[4],
            "avg_execution_time": result[5],
            "last_executed": result[6],
        })

    return {"functions": metrics_data}
