# Ambiente de desenvolvimento

Este documento registra como preparar o ambiente local do projeto.

## Requisitos

- Docker.
- Docker Compose.

O container usa Python 3.12 e instala as dependencias registradas em `requirements.txt`.

## Variaveis locais

O projeto usa um arquivo `.env` local para configurar os valores padrao da CLI.

Criar a partir do exemplo:

```bash
cp .env.example .env
```

Variaveis disponiveis:

- `COTACOES_SOURCE`: fonte padrao da coleta.
- `COTACOES_SOURCES_FILE`: arquivo com as fontes disponiveis.
- `COTACOES_RAW_DIR`: diretorio para salvar HTML bruto.
- `COTACOES_DATABASE_PATH`: caminho do arquivo SQLite.
- `COTACOES_HTTP_TIMEOUT_SECONDS`: timeout das requisicoes HTTP.
- `COTACOES_REQUEST_DELAY_SECONDS`: intervalo minimo entre requisicoes HTTP.
- `COTACOES_REUSE_RAW_BEFORE_REQUEST`: quando `true`, usa HTML ja salvo na pasta raw principal antes de fazer nova requisicao.
- `COTACOES_TARGET_DATE`: data alvo da coleta. Se vazio, usa a data atual do sistema. Aceita `DD/MM/YYYY` ou `YYYY-MM-DD`.
- `COTACOES_QUOTES_BACK`: quantidade de datas de cotacao anteriores para coletar a partir da data alvo.

Exemplos:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=0
```

Coleta somente a cotacao da data atual.

```env
COTACOES_TARGET_DATE=29/05/2026
COTACOES_QUOTES_BACK=0
```

Coleta somente a cotacao de `29/05/2026`.

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=30
```

Coleta a data atual e mais 30 datas de cotacao anteriores encontradas. Isso nao significa 30 dias corridos.

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```

Antes de baixar uma pagina de cotacao, tenta reutilizar o HTML correspondente em `data/raw/<fonte>/`. A busca nao considera `old/` nem arquivos compactados.

O `.env` nao deve ser versionado. O arquivo versionavel e o `.env.example`.

## Dependencias

O projeto usa dependencias externas para leitura e parsing de HTML e PDF.

As dependencias sao instaladas dentro da imagem Docker durante o build:

```bash
docker compose build
```

Dependencias principais:

- `beautifulsoup4`: extracao de dados do HTML.
- `lxml`: parser HTML usado pelo BeautifulSoup.
- `pypdf`: extracao de texto dos PDFs da CEASA-PR.

## Executar coleta

Ver comandos de execucao em [Comandos](comandos.md).

Baixar os arquivos brutos de todas as categorias:

```bash
docker compose run --rm baixar
```

Processar os arquivos brutos baixados e salvar no SQLite:

```bash
docker compose run --rm banco
```

Baixar, processar e salvar no SQLite em um unico comando:

```bash
docker compose run --rm tudo
```

Compactar HTMLs antigos da pasta `old`:

```bash
docker compose run --rm compactar-old
```

Esse comando gera um novo `.zip` dentro de `data/raw/<fonte>/old/` e remove os `.html` compactados.

O compose monta a raiz do projeto em `/app`. Com isso, o `.env`, `config/` e os arquivos gerados em `data/` ficam no mesmo diretorio do projeto.
