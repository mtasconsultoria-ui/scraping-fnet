import { Pool } from "pg";

/**
 * Pool compartilhado entre invocações da mesma instância serverless.
 *
 * Em ambiente serverless cada instância abre poucas conexões; com Neon/Supabase
 * use a connection string *pooled* (PgBouncer) para não esgotar o limite do
 * banco quando a Vercel escalar as funções.
 */
declare global {
  // eslint-disable-next-line no-var
  var __fnetPool: Pool | undefined;
}

export function getPool(): Pool {
  if (!global.__fnetPool) {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
      throw new Error("DATABASE_URL não configurada");
    }
    global.__fnetPool = new Pool({
      // aceita a mesma URL usada pelo ingestor Python (dialeto do SQLAlchemy)
      connectionString: connectionString.replace(/^postgresql\+psycopg:/, "postgresql:"),
      max: Number(process.env.PG_POOL_MAX ?? 5),
      ssl: process.env.PGSSL === "disable" ? undefined : { rejectUnauthorized: false },
    });
  }
  return global.__fnetPool;
}

export async function query<T extends Record<string, unknown>>(
  text: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await getPool().query(text, params);
  return result.rows as T[];
}
