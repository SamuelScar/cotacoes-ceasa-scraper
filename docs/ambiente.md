# Ambiente e configuracao

## Requisitos

- Docker.
- Docker Compose.

O container usa Python 3.12 e instala as dependencias declaradas em
`pyproject.toml`.
Ele tambem instala `pigz`, usado pelo wrapper operacional para compactar e
descompactar `data.tar.gz` com multiplas threads.
Os horarios dos relatorios usam o fuso `America/Sao_Paulo`.

## Preparacao

```bash
cp .env.example .env
docker compose build
```

O Compose monta a raiz do projeto em `/app`. Configuracoes e arquivos gerados
em `data/` permanecem na maquina.

## Variaveis locais

O `.env` define os valores padrao da CLI. Quando existe uma opcao equivalente,
argumentos passados ao servico `app` sobrescrevem esses valores.

| Variavel | Uso |
| --- | --- |
| `COTACOES_SOURCES_FILE` | Arquivo JSON com as fontes |
| `COTACOES_RAW_DIR` | Diretorio dos arquivos brutos |
| `COTACOES_DATABASE_PATH` | Caminho do SQLite |
| `COTACOES_SUPABASE_DATABASE_URL` | Connection string PostgreSQL do Supabase |
| `COTACOES_SUPABASE_BATCH_SIZE` | Quantidade de registros enviada por lote |
| `COTACOES_HTTP_TIMEOUT_SECONDS` | Timeout de cada requisicao |
| `COTACOES_REQUEST_DELAY_SECONDS` | Intervalo minimo entre requisicoes |
| `COTACOES_REUSE_RAW_BEFORE_REQUEST` | Reutiliza raw ativo antes de baixar |
| `COTACOES_INCREMENTAL_HISTORY` | Continua o historico antes do raw mais antigo |
| `COTACOES_COMPLEMENT_PROHORT` | Executa o complemento PROHORT depois de salvar |
| `COTACOES_TARGET_DATE` | Data limite em `DD/MM/YYYY` ou `YYYY-MM-DD` |
| `COTACOES_QUOTES_BACK` | Quantidade de cotacoes anteriores ou `infinito` |

Exemplos:

```env
# Ultima cotacao disponivel
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=0

# Data limite e mais 30 cotacoes anteriores
COTACOES_TARGET_DATE=29/05/2026
COTACOES_QUOTES_BACK=30

# Todo o historico encontrado, da data mais nova para a mais antiga
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=infinito
```

`COTACOES_QUOTES_BACK` conta datas de cotacao encontradas, nao dias corridos.
No modo `infinito`, a busca termina depois de 366 tentativas consecutivas sem
encontrar uma data mais antiga.
No fluxo de todas as fontes, a janela e zerada para fontes sem historico. Em
uma execucao isolada, informar `--quotes-back` para essas fontes gera erro.

Com `COTACOES_INCREMENTAL_HISTORY=true` e uma janela historica maior que zero,
a busca comeca antes da data mais antiga representada nos raws ativos da fonte.
Se nao houver raw historico, a busca comeca pela cotacao mais recente. Uma
`COTACOES_TARGET_DATE` informada manualmente tem prioridade sobre o modo
incremental. Com `COTACOES_QUOTES_BACK=0`, o modo incremental fica inativo para
que a publicacao atual continue sendo atualizada normalmente.

Com `COTACOES_REUSE_RAW_BEFORE_REQUEST=true`, o coletor procura o arquivo
correspondente diretamente em `data/raw/<fonte>/`. A busca nao usa `old/` nem
arquivos compactados.

Com `COTACOES_COMPLEMENT_PROHORT=true`, todo fluxo que salva no SQLite executa
o complemento PROHORT uma vez ao final. A URL versionada fica em
`config/prohort.json`, nao no `.env`.

O `.env` e local e nao deve ser versionado. Atualize `.env.example` quando uma
nova configuracao obrigatoria for adicionada.

Sem `--source`, a CLI executa todas as fontes presentes em
`COTACOES_SOURCES_FILE`. Para executar somente uma fonte, informe-a diretamente
na chamada, por exemplo: `--source ceasa-pe`.
