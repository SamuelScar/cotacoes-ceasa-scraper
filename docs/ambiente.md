# Ambiente de desenvolvimento

Este documento registra como preparar o ambiente local do projeto.

## Requisitos

- Python 3.11 ou superior.
- `venv`, disponivel na biblioteca padrao do Python.

O ambiente criado neste projeto foi validado com Python 3.12.3.

## Criar ambiente virtual

Na raiz do projeto:

```bash
python3 -m venv .venv
```

## Ativar ambiente

```bash
source .venv/bin/activate
```

Depois de ativado, o comando `python` deve apontar para o Python do `.venv`.

## Variaveis locais

O projeto usa um arquivo `.env` local para configurar os valores padrao da CLI.

Criar a partir do exemplo:

```bash
cp .env.example .env
```

Variaveis disponiveis:

- `COTACOES_SOURCE`: fonte padrao da coleta.
- `COTACOES_CATEGORY`: categoria padrao da fonte. Use `todas` para descobrir categorias automaticamente.
- `COTACOES_SOURCES_FILE`: arquivo com as fontes disponiveis.
- `COTACOES_RAW_DIR`: diretorio para salvar HTML bruto.
- `COTACOES_DATABASE_PATH`: caminho do arquivo SQLite.
- `COTACOES_HTTP_TIMEOUT_SECONDS`: timeout das requisicoes HTTP.
- `COTACOES_REQUEST_DELAY_SECONDS`: intervalo minimo entre requisicoes HTTP.
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

O `.env` nao deve ser versionado. O arquivo versionavel e o `.env.example`.

## Dependencias

O projeto usa dependencias externas para leitura e parsing de HTML.

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Dependencias principais:

- `beautifulsoup4`: extracao de dados do HTML.
- `lxml`: parser HTML usado pelo BeautifulSoup.

## Executar coleta

Ver comandos de execucao em [Comandos](comandos.md).

Com o ambiente ativado, um teste simples da CLI e:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --help
```
