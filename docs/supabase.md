# Sincronizacao com Supabase

O comando de sincronizacao envia o banco SQLite local para o PostgreSQL do
Supabase, preservando IDs e relacionamentos.

Configure no `.env` a connection string obtida em **Connect > Direct > Session
pooler**:

```env
COTACOES_SUPABASE_DATABASE_URL=postgresql://...
```

A credencial deve permanecer somente no `.env` local.

Para criar as tabelas e sincronizar os registros:

```bash
docker compose build
docker compose run --rm sincronizar-supabase
```

O comando:

- cria as tabelas e indices quando ainda nao existem;
- insere registros novos;
- atualiza registros existentes com o mesmo `id`;
- nao apaga registros existentes no Supabase.

As tabelas sao sincronizadas na ordem das chaves estrangeiras. A operacao usa
uma unica transacao: se ocorrer uma falha, nenhuma alteracao parcial e
confirmada no Supabase.

O Row Level Security (RLS) e habilitado em todas as tabelas. A conexao direta
continua funcionando, mas o acesso pela API do Supabase exige politicas
definidas explicitamente.
