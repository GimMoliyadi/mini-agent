"""Disposable SQLite task API used as an autonomous development input."""

from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(__file__).with_name("tasks.db")
app = FastAPI(title="Task Acceptance")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    with connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")


initialize()


class TaskInput(BaseModel):
    title: str


class Task(TaskInput):
    id: int


@app.get("/api/tasks", response_model=list[Task])
def list_tasks():
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY id")]


@app.post("/api/tasks", response_model=Task)
def create_task(task: TaskInput):
    with connect() as connection:
        cursor = connection.execute("INSERT INTO tasks (title) VALUES (?)", (task.title,))
        return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.put("/api/tasks/{task_id}", response_model=Task)
def update_task(task_id: int, task: TaskInput):
    with connect() as connection:
        cursor = connection.execute("UPDATE tasks SET title=? WHERE id=?", (task.title, task_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Task not found")
        return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with connect() as connection:
        cursor = connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Task not found")
    return {"deleted": task_id}
