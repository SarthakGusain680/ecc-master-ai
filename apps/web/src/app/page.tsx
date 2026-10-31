"use client";
import { useState } from "react";

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [logs, setLogs] = useState<any[]>([]);
  const [status, setStatus] = useState("idle");
  const [finalAnswer, setFinalAnswer] = useState("");

  const API_URL = process.env.NEXT_PUBLIC_AGENT_URL || "https://ecc-master-ai.vercel.app";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("running");
    setLogs([]);
    setFinalAnswer("");
    
    try {
      const res = await fetch(`${API_URL}/start_job`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      const data = await res.json();
      pollJob(data.job_id);
    } catch (err) {
      console.error(err);
      setStatus("failed");
    }
  };

  const pollJob = async (id: string) => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/get_job?job_id=${id}`);
        const data = await res.json();
        
        if (data.logs) {
          setLogs(data.logs);
          const final = data.logs.find((l: any) => l.log_type === "final_answer");
          if (final) {
            setFinalAnswer(final.content.text);
            setStatus("completed");
            clearInterval(interval);
          }
        }
        
        if (data.job && data.job[0]?.status === "completed") {
           setStatus("completed");
           clearInterval(interval);
        } else if (data.job && data.job[0]?.status === "failed") {
           setStatus("failed");
           clearInterval(interval);
        }
      } catch (err) {
        console.error("Polling error", err);
      }
    }, 2000);
  };

  return (
    <main className="min-h-screen bg-gray-950 text-gray-100 p-8">
      <div className="max-w-3xl mx-auto">
        <h1 className="text-3xl font-bold mb-2 text-green-400">ECC Master AI 🧠</h1>
        <p className="text-gray-400 mb-8">Serverless ReAct Agent • Groq + Gemini • $0.00/mo</p>
        
        <form onSubmit={handleSubmit} className="mb-8">
          <textarea
            className="w-full p-4 rounded-lg bg-gray-900 border border-gray-800 text-white focus:ring-2 focus:ring-green-500 outline-none"
            rows={4}
            placeholder="e.g., Create a python script called test.py that prints 'Hello' and run it."
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            disabled={status === "running"}
          />
          <button 
            type="submit" 
            disabled={status === "running" || !prompt.trim()}
            className="mt-4 px-6 py-3 bg-green-600 hover:bg-green-700 disabled:bg-gray-700 rounded-lg font-semibold"
          >
            {status === "running" ? "Thinking..." : "Run Agent"}
          </button>
        </form>

        {status === "running" && (
          <div className="mb-8 p-4 bg-gray-900 rounded-lg border border-gray-800">
            <h3 className="text-sm font-semibold text-yellow-400 mb-3">Live Agent Thoughts:</h3>
            <div className="space-y-2 max-h-64 overflow-y-auto font-mono text-xs">
              {logs.map((log, i) => (
                <div key={i} className="p-2 bg-gray-950 rounded border border-gray-800">
                  <span className="text-blue-400 font-bold">[{log.log_type}]</span>{" "}
                  <span className="text-gray-300">
                    {JSON.stringify(log.content).substring(0, 150)}...
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {finalAnswer && (
          <div className="p-6 bg-gray-900 rounded-lg border border-green-500/30">
            <h3 className="text-lg font-semibold text-green-400 mb-3">✅ Final Answer:</h3>
            <pre className="whitespace-pre-wrap text-sm">{finalAnswer}</pre>
          </div>
        )}
      </div>
    </main>
  );
}