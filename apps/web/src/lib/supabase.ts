import { createClient, SupabaseClient } from "@supabase/supabase-js";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";

// Browser-safe client (returns null if env vars are missing)
export const supabase: SupabaseClient | null = 
  supabaseUrl.startsWith("http") && supabaseAnonKey 
    ? createClient(supabaseUrl, supabaseAnonKey) 
    : null;

// Server-only client
export function createServerClient(): SupabaseClient | null {
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!supabaseUrl.startsWith("http") || !serviceRoleKey) return null;
  
  return createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false },
  });
}