import { NextResponse } from "next/server";
import { createServerClient } from "@/lib/supabase";

export const dynamic = "force-dynamic"; 

export async function GET() {
  const started = Date.now();
  try {
    const db = createServerClient();
    
    // If db is null, it means env vars are missing
    if (!db) {
      return NextResponse.json(
        { status: "unhealthy", error: "Missing or invalid Supabase env vars in .env.local" },
        { status: 500 }
      );
    }

    const { error } = await db.from("instincts").select("id").limit(1);
    if (error) throw error;

    return NextResponse.json({
      status: "healthy",
      service: "vercel-nextjs",
      supabase: "connected",
      latencyMs: Date.now() - started,
    });
  } catch (e: any) {
    return NextResponse.json(
      { status: "unhealthy", error: e.message || String(e) },
      { status: 500 }
    );
  }
}