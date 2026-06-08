# Ambiente e configuracao

## Requisitos

- Docker.
- Docker Compose.

O container usa Python 3.12 e instala as dependencias de `requirements.txt`.

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
| `COTACOES_SOURCE` | Fonte padrao para comandos avancados |
| `COTACOES_SOURCES_FILE` | Arquivo JSON com as fontes |
| `COTACOES_RAW_DIR` | Diretorio dos arquivos brutos |
| `COTACOES_DATABASE_PATH` | Caminho do SQLite |
| `COTACOES_HTTP_TIMEOUT_SECONDS` | Timeout de cada requisicao |
| `COTACOES_REQUEST_DELAY_SECONDS` | Intervalo minimo entre requisicoes |
| `COTACOES_REUSE_RAW_BEFORE_REQUEST` | Reutiliza raw ativo antes de baixar |
| `COTACOES_PROHORT_URL` | URL do `ProhortDiario.txt` |
| `COTACOES_TARGET_DATE` | Data limite em `DD/MM/YYYY` ou `YYYY-MM-DD` |
| `COTACOES_QUOTES_BACK` | Quantidade de cotacoes anteriores |

Exemplos:

```env
# Ultima cotacao disponivel
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=0

# Data limite e mais 30 cotacoes anteriores
COTACOES_TARGET_DATE=29/05/2026
COTACOES_QUOTES_BACK=30
```

`COTACOES_QUOTES_BACK` conta datas de cotacao encontradas, nao dias corridos.
No fluxo de todas as fontes, a janela e zerada para fontes sem historico. Em
uma execucao isolada, informar `--quotes-back` para essas fontes gera erro.

Com `COTACOES_REUSE_RAW_BEFORE_REQUEST=true`, o coletor procura o arquivo
correspondente diretamente em `data/raw/<fonte>/`. A busca nao usa `old/` nem
arquivos compactados.

O `.env` e local e nao deve ser versionado. Atualize `.env.example` quando uma
nova configuracao obrigatoria for adicionada.
