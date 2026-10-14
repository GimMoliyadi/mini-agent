import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

type Task = { id: number; title: string };
const base = "http://127.0.0.1:8000";
async function request(path: string, options?: RequestInit) {
  const response = await fetch(base + path, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
function App() {
  const [title, setTitle] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const client = useQueryClient();
  const { data = [], error } = useQuery<Task[]>({ queryKey: ["tasks"], queryFn: () => request("/api/tasks") });
  const save = useMutation({
    mutationFn: () => request(editing === null ? "/api/tasks" : `/api/tasks/${editing}`, {
      method: editing === null ? "POST" : "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ["tasks"] }); setTitle(""); setEditing(null); },
  });
  return <main><h1>Tasks</h1>
    <form onSubmit={event => { event.preventDefault(); save.mutate(); }}>
      <label>Title<input id="title" value={title} onChange={event => setTitle(event.target.value)} required /></label>
      <button id="save" type="submit">{editing === null ? "Create" : "Save"}</button>
    </form>
    {(error || save.error) && <p id="error">Request failed</p>}
    <ul id="tasks">{data.map(task => <li key={task.id} id={`task-${task.id}`}>
      <span>{task.title}</span><button id={`edit-${task.id}`} onClick={() => { setEditing(task.id); setTitle(task.title); }}>Edit</button>
    </li>)}</ul>
  </main>;
}
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={new QueryClient()}><App /></QueryClientProvider>);
