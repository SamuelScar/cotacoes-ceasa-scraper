# Ambiente e configuracao

## Requisitos

- Docker.
- Docker Compose.

O container usa Python 3.12 e instala as dependencias declaradas em
`pyproject.toml`.
Ele tambem instala `xz` e `pigz`, usados pelo script de pacote executado dentro do container para compactar e descompactar os artefatos de dados. Os novos pacotes usam `.xz`; `.gz` fica suportado para restaurar artefatos legados.
Os horarios dos relatorios usam o fuso `America/Sao_Paulo`.

## Preparacao

```bash
cp .env.example .env
docker compose build
```

O Compose monta a raiz do projeto em `/app`. Configuracoes e arquivos gerados
em `data/` permanecem na maquina.

## Ambiente do crawler

O crawler atual roda no GitHub Actions pelo workflow
`.github/workflows/scraper-release.yml`. Ele cria um `.env` temporario no runner,
restaura o pacote completo pelo OneDrive quando configurado, usa o banco da release `latest-data` como fallback, executa o servico `tudo` e tenta salvar os dados gerados antes de encerrar a rodada.

O GitHub Release publica somente `cotacoes.sqlite.xz`, pronto para consumo. O pacote do OneDrive preserva o backup completo, com raws, cache, relatorios e SQLite.

As variaveis usadas pelo agendamento ficam no environment `Crawler`, em
**Settings > Environments > Crawler > Environment variables**. O disparo manual
tambem aceita os mesmos valores como campos opcionais em **Run workflow**.
Secrets SMTP sao opcionais e ficam em **Environment secrets**, apenas para envio
de relatorio por e-mail.

Para manter uma copia no OneDrive fora do GitHub, configure no environment
`Crawler`:

| Chave | Tipo | Uso |
| --- | --- | --- |
| `DATA_ONEDRIVE_BACKUP_ENABLED` | Variable | Use `true` para ativar o backup no OneDrive |
| `DATA_ONEDRIVE_ASSET_NAME` | Variable | Nome do pacote completo. Padrao: `ceasa-data-full-latest.tar.xz` |
| `DATA_ONEDRIVE_REMOTE` | Variable | Nome do remote no `rclone`. Padrao: `onedrive` |
| `DATA_ONEDRIVE_DIR` | Variable | Diretorio base no OneDrive. Padrao: `cotacoes-ceasa` |
| `RCLONE_CONFIG` | Secret | Conteudo do arquivo `rclone.conf` com acesso ao OneDrive |

O workflow instala `rclone` no runner, grava o `RCLONE_CONFIG` temporariamente e
sincroniza o pacote em `latest/` e `history/` dentro de `DATA_ONEDRIVE_DIR`.
Se `DATA_ONEDRIVE_BACKUP_ENABLED` estiver ausente, estiver `false` ou o secret
`RCLONE_CONFIG` nao existir, o workflow nao tenta usar OneDrive e segue pelo
fluxo com o banco do GitHub Release.

Para gerar o secret `RCLONE_CONFIG`, configure o remote uma vez na sua maquina:

```bash
rclone config
rclone config file
```

O remote precisa ter o mesmo nome de `DATA_ONEDRIVE_REMOTE`, por padrao
`onedrive`. Depois copie o conteudo do arquivo `rclone.conf` indicado pelo
comando e salve em **Environment secrets > RCLONE_CONFIG**.

## Variaveis locais

O `.env` define os valores padrao da CLI. Quando existe uma opcao equivalente,
argumentos passados ao servico `app` sobrescrevem esses valores.

| Variavel | Uso |
| --- | --- |
| `COTACOES_SOURCES_FILE` | Arquivo JSON com as fontes |
| `COTACOES_RAW_DIR` | Diretorio dos arquivos brutos |
| `COTACOES_PDF_TEXT_CACHE_DIR` | Diretorio do cache de texto extraido de PDFs |
| `COTACOES_DATABASE_PATH` | Caminho do SQLite |
| `COTACOES_SUPABASE_DATABASE_URL` | Connection string PostgreSQL do Supabase |
| `COTACOES_SUPABASE_BATCH_SIZE` | Quantidade de registros enviada por lote |
| `COTACOES_HTTP_TIMEOUT_SECONDS` | Timeout de cada requisicao |
| `COTACOES_REQUEST_DELAY_SECONDS` | Intervalo minimo entre requisicoes |
| `COTACOES_WORKERS` | Quantidade de fontes baixadas em paralelo |
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

`COTACOES_PDF_TEXT_CACHE_DIR` define onde ficam os textos extraidos de PDFs. O
padrao e `data/cache/pdf-text/`; apagar esse diretorio nao remove raws nem
registros do SQLite, apenas força nova extracao de texto dos PDFs.

Com `COTACOES_COMPLEMENT_PROHORT=true`, todo fluxo que salva no SQLite executa
o complemento PROHORT uma vez ao final. A URL versionada fica em
`config/prohort.json`, nao no `.env`.
No workflow do GitHub Actions, defina essa variavel no Environment `Crawler`
para habilitar o complemento na execucao automatica.

O `.env` e local e nao deve ser versionado. Atualize `.env.example` quando uma
nova configuracao obrigatoria for adicionada.

Sem `--source`, a CLI executa todas as fontes presentes em
`COTACOES_SOURCES_FILE`. Para executar somente uma fonte, informe-a diretamente
na chamada, por exemplo: `--source ceasa-pe`.
Nos comandos `baixar` e `tudo`, `COTACOES_WORKERS` controla quantas fontes
baixam em paralelo. O valor `1` preserva a execucao sequencial.
