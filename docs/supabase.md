# Sincronizacao com Supabase

O comando de sincronizacao envia o banco SQLite local para o PostgreSQL do
Supabase, preservando IDs e relacionamentos.

Configure no `.env` a connection string obtida em **Connect > Direct > Session
pooler**:

```env
COTACOES_SUPABASE_DATABASE_URL=postgresql://...
```

A credencial deve permanecer somente no `.env` local.

## Sincronizacao incremental

Use na operacao normal para adicionar os registros criados desde a ultima
sincronizacao:

```bash
docker compose build
docker compose run --rm sincronizar-supabase
```

O modo incremental:

- cria as tabelas e indices quando ainda nao existem;
- nao limpa as tabelas remotas;
- adiciona novas coletas e cotacoes;
- reenvia as tabelas pequenas de referencia para manter seus dados atualizados;
- exige um destino vazio ou alinhado por uma sincronizacao completa anterior.

Alteracoes em coletas ou cotacoes antigas nao sao detectadas pelo modo
incremental. Use a substituicao completa quando precisar reconstruir esses
registros.

## Substituicao completa

Use excepcionalmente para substituir todo o snapshot remoto:

```bash
docker compose run --rm substituir-supabase
```

O modo completo:

- limpa somente as tabelas gerenciadas pelo scraper;
- preserva os mesmos IDs e relacionamentos do SQLite;
- nao altera outras tabelas existentes no projeto Supabase.

## Lotes e retomada

Os dois modos sincronizam na ordem das chaves estrangeiras e usam lotes de
`5.000` registros por padrao. O tamanho pode ser reduzido no `.env`:

```env
COTACOES_SUPABASE_BATCH_SIZE=1000
```

Cada lote concluido e confirmado separadamente. O progresso fica registrado em
`cotacoes_sync_runs` e `cotacoes_sync_watermarks`.

Se a conexao cair, execute o mesmo comando novamente. A sincronizacao continua
da tabela e do ultimo ID confirmados, sem limpar novamente o snapshot remoto.
Um modo incompleto deve ser retomado antes de iniciar o outro.

Durante uma sincronizacao completa interrompida, o Supabase pode permanecer com
um snapshot parcial ate a retomada terminar. Indices, constraints, RLS,
politicas e triggers permanecem preservados.

## Migracao excepcional com pgloader

O `pgloader` esta integrado como alternativa para uma migracao completa
excepcional:

```bash
docker compose run --rm migrar-supabase-pgloader
```

Ele transmite os dados com `COPY` e processamento interno em lotes, gerando um
relatorio proprio em `data/relatorios/`. O schema gerenciado pelo projeto
precisa ja existir no Supabase, por exemplo apos ao menos uma tentativa do
comando `substituir-supabase`.

Ao terminar, o comando registra os marcadores necessarios para as proximas
sincronizacoes incrementais. Diferentemente dos dois comandos principais, uma
migracao interrompida com `pgloader` deve ser reiniciada desde o inicio.

O Row Level Security (RLS) e habilitado em todas as tabelas. A conexao direta
continua funcionando, mas o acesso pela API do Supabase exige politicas
definidas explicitamente.
