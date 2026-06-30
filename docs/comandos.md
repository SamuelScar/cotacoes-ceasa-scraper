# Comandos

## Comandos principais

Estes sao os comandos usados na operacao normal. A pasta `data/` fica fora
do Git e pode ser empacotada ou restaurada com `python scripts/cotacoes.py`
quando necessario.

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws sem processar |
| `docker compose run --rm salvar` | Processa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Baixa e persiste somente os raws da coleta |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm sincronizar-supabase` | Adiciona novos registros ao Supabase |
| `docker compose run --rm substituir-supabase` | Substitui completamente o Supabase |
| `docker compose run --rm migrar-supabase-pgloader` | Executa migracao completa excepcional |
| `docker compose run --rm compactar-old` | Compacta HTMLs soltos de `old/` |

Data limite, janela historica, caminhos, delay e timeout sao lidos do `.env`.
Uma falha em uma fonte nao interrompe o lote das demais.
Sem `--source`, a CLI executa todas as fontes presentes em `config/fontes.json`.
Nos comandos `baixar` e `tudo`, `COTACOES_WORKERS` ou `--workers` definem
quantas fontes podem baixar ao mesmo tempo. O padrao `1` mantem o fluxo
sequencial. No `tudo`, quando ha mais de um worker, a persistencia entra em
pipeline: uma fonte que terminou o download e enviada para processamento
enquanto as outras continuam baixando.

Com `COTACOES_INCREMENTAL_HISTORY=true`, pedidos de historico continuam antes
do raw ativo mais antigo de cada fonte. A coleta atual com `quotes_back=0`
permanece inalterada.

A saida usa cores em terminais interativos e informa cada raw assim que ele e
salvo. Para desativar as cores, execute o container com `-e NO_COLOR=1`.
Comandos longos exibem progresso por fonte, categoria ou arquivo. Em terminal
interativo, a barra usa Rich; fora de TTY, o progresso aparece como linhas de
texto e tambem fica registrado no relatorio.

Cada comando gera um relatorio em `data/relatorios/`, inclusive quando termina
com erro ou e interrompido. O arquivo registra somente as fases executadas e
inclui configuracoes sem credenciais, duracao, resultados por fonte, alertas,
erros e historico cronologico.
Por padrao, o historico nao registra uma linha `OK` para cada raw processado;
os totais por fonte continuam no resumo. Use `--raw-detail-report` quando
precisar auditar arquivo por arquivo.

## Selecionar fontes

Sem `--source`, o servico `app` tambem executa todas as fontes:

```bash
docker compose run --rm app
```

Use o servico `app` quando quiser executar somente uma fonte.

```bash
# Baixar o raw e extrair as cotacoes sem salvar no SQLite
docker compose run --rm app --source ceasa-pe

# Baixar, extrair e salvar no SQLite
docker compose run --rm app --source ceasa-pr --save

# Coletar uma data limite e mais 30 cotacoes anteriores
docker compose run --rm app --source ceasa-rj --target-date 03/06/2026 --quotes-back 30 --save

# Listar categorias descobertas
docker compose run --rm app --source ceasa-pe --list-categories
```

Fontes e limitacoes de historico estao em [Fontes e limitacoes](fontes.md).

## Modos do servico `app`

| Comando isolado | Baixa raw | Extrai cotacoes | Salva no SQLite |
| --- | --- | --- | --- |
| `app --source <fonte>` | Sim | Sim | Nao |
| `app --source <fonte> --save` | Sim | Sim | Sim |
| `app --source <fonte> --process-raw` | Nao | Sim | Sim |

O comportamento padrao valida a extracao sem alterar o banco. `--save`
persiste as cotacoes e `--process-raw` reprocessa os arquivos ativos sem
acessar a fonte.

O fluxo `tudo` processa somente os arquivos selecionados pelo download da
execucao atual. Assim, `quotes_back` tambem limita o volume da persistencia.
Com `COTACOES_WORKERS` maior que `1`, os downloads rodam em paralelo e os
resultados concluidos entram em uma fila de persistencia em memoria. Essa fila
guarda somente o resultado da fonte e os caminhos dos raws; os arquivos HTML e
PDF continuam em disco. Um unico consumidor processa e salva uma fonte por vez
no SQLite, preservando o relatorio completo para depuracao.

Se uma fonte falhar depois de baixar parte dos raws, o proprio `tudo` processa
esses arquivos parciais e mantem a falha registrada no relatorio. Se a execucao
inteira for interrompida antes de algum raw entrar na persistencia, execute
`docker compose run --rm salvar` para aproveitar os raws ja salvos.

## Parametros uteis

| Opcao | Uso |
| --- | --- |
| `--source` | Limita a execucao a uma fonte; sem ele executa todas |
| `--target-date` | Define a data limite |
| `--quotes-back` | Define quantas cotacoes anteriores buscar |
| `--list-categories` | Lista categorias descobertas |
| `--raw-dir` | Sobrescreve o diretorio de raws |
| `--pdf-text-cache-dir` | Sobrescreve o diretorio do cache de texto extraido de PDFs |
| `--database-path` | Sobrescreve o caminho do SQLite |
| `--http-timeout-seconds` | Sobrescreve o timeout HTTP |
| `--request-delay-seconds` | Sobrescreve o intervalo entre requisicoes |
| `--workers` | Define quantas fontes baixam em paralelo nos fluxos de todas as fontes |
| `--force-reprocess` | Reprocessa raws mesmo quando ja existem no SQLite com o mesmo hash |
| `--raw-detail-report` | Registra cada raw processado no historico completo do relatorio |

Os flags `--download-only`, `--download-and-process`, `--archive-raw-old` e
`--complement-prohort` sao usados internamente pelos atalhos definidos no
Compose. Nao e necessario usa-los diretamente.

Para baixar fontes em paralelo em uma chamada pontual:

```bash
docker compose run --rm app --download-only --workers 3
docker compose run --rm app --download-and-process --workers 3
```

Mesmo com `--workers`, cada fonte continua sequencial internamente. No `tudo`,
a persistencia pode comecar antes de todos os downloads terminarem, mas o
SQLite continua sem escrita concorrente.

Para usar os atalhos `baixar` e `tudo`, configure `COTACOES_WORKERS=3` no
`.env` e execute os comandos normais.

Para consultar todas as opcoes:

```bash
docker compose run --rm app --help
```

## Raws e reprocessamento

Arquivos ativos ficam em `data/raw/<fonte>/`. Quando outro arquivo do mesmo
grupo e gerado no mesmo dia, a versao anterior vai para `old/`.

O comando `salvar` processa somente arquivos `.html` e `.pdf` diretamente na
pasta da fonte. Ele ignora `old/` e `.zip`.

Durante o processamento, raws que ja existem em `coletas` com o mesmo
`arquivo_raw` e `hash_raw` sao ignorados automaticamente. Isso evita repetir
parser e normalizacao quando o dado ja foi persistido.

Use `--force-reprocess` quando precisar validar novamente todos os raws ativos.
Na operacao normal, `tudo` processa somente os raws selecionados na coleta
atual.

Textos extraidos de PDFs sao cacheados em `data/cache/pdf-text/` por padrao. O
cache e indexado pelo hash do PDF e pela versao da estrategia de extracao. Se o
PDF mudar, o texto e extraido novamente.

Para reconstruir o banco depois de uma mudanca de schema ou normalizacao:

```bash
rm data/cotacoes.sqlite
docker compose run --rm app --process-raw --force-reprocess
```

O projeto nao migra bancos antigos. Confirme que os raws ativos necessarios
estao presentes antes de excluir o SQLite.

## Pacote de dados

A pasta `data/` nao e versionada no Git. O pacote mais recente deve ficar como
asset da release `latest-data`, com o nome `ceasa-data-latest.tar.gz`.

Use o script abaixo para compactar ou restaurar esse pacote:

```bash
python scripts/cotacoes.py compactar
python scripts/cotacoes.py descompactar
python scripts/cotacoes.py compactar --arquivo ceasa-data-latest.tar.gz
python scripts/cotacoes.py descompactar --arquivo ceasa-data-latest.tar.gz
```

O script faz somente a operacao de pacote:

1. cria um lock para impedir duas operacoes simultaneas no pacote;
2. em `compactar`, compacta `data/` em um `.tar.gz.tmp` com `tar` e `pigz`;
3. valida o pacote temporario;
4. substitui o `.tar.gz` final somente depois da validacao;
5. em `descompactar`, restaura a pasta `data/` a partir do `.tar.gz` informado.

O `pigz` roda dentro do container e usa multiplas threads para compactar e
descompactar mais rapido. O host precisa apenas de Python, Docker e Docker
Compose.

Para inspecionar manualmente o pacote sem descompactar:

```bash
docker compose run --rm --entrypoint tar app -I pigz -tf ceasa-data-latest.tar.gz
```

## Crawler por workflow

O crawler atual do projeto e o workflow `.github/workflows/scraper-release.yml`.
Ele usa o GitHub Actions como agendador e a release `latest-data` como ponto de
persistencia do pacote `data/`.

Fluxo executado pelo workflow:

1. selecionar a janela diaria de execucao;
2. criar o `.env` sem credenciais pelo action local `prepare-scraper`;
3. construir a imagem Docker;
4. baixar e descompactar `ceasa-data-latest.tar.gz` da release fixa, se existir;
5. executar `docker compose run --rm tudo`;
6. compactar `data/` novamente com `python scripts/cotacoes.py compactar`;
7. substituir o asset `ceasa-data-latest.tar.gz` na release `latest-data`;
8. enviar o ultimo relatorio por e-mail se os secrets SMTP estiverem presentes.

Variaveis principais do workflow:

| Variavel | Uso atual |
| --- | --- |
| `DATA_RELEASE_TAG` | Tag da release fixa. Padrao: `latest-data` |
| `DATA_ASSET_NAME` | Nome do pacote publicado. Padrao: `ceasa-data-latest.tar.gz` |
| `COTACOES_TARGET_DATE` | Data limite usada no runner. Padrao: vazio |
| `COTACOES_QUOTES_BACK` | Janela historica usada no runner. Padrao: `100` |
| `COTACOES_INCREMENTAL_HISTORY` | Define se a coleta continua antes do raw mais antigo. Padrao: `false` |
| `COTACOES_WORKERS` | Quantidade de fontes baixadas em paralelo. Padrao: `1` |
| `COTACOES_REQUEST_DELAY_SECONDS` | Delay entre requisicoes HTTP. Padrao: `7.0` |

No disparo manual, esses valores aparecem como campos opcionais em
**Actions > Atualizar pacote de dados > Run workflow**. Nas execucoes agendadas,
o workflow le as mesmas chaves no environment `Crowler`, em **Settings >
Environments > Crowler > Environment variables**. Se uma variavel nao existir, o
padrao acima e usado.

Esse workflow substitui, por enquanto, a ideia de manter um container `crawler`
rodando continuamente. O estado entre execucoes fica no pacote da release e a
idempotencia continua dependendo do SQLite, dos raws salvos e das chaves unicas
de persistencia.

## Complemento PROHORT

Para complementar automaticamente depois de qualquer fluxo que salva no
SQLite, configure:

```env
COTACOES_COMPLEMENT_PROHORT=true
```

O comando abaixo permanece disponivel para executar somente o complemento sob
demanda:

```bash
docker compose run --rm complementar-prohort
```

O complemento preenche dados apenas quando encontra correspondencia confiavel,
nao sobrescreve campos preenchidos e registra sua origem.

A URL do arquivo `ProhortDiario.txt` fica em `config/prohort.json`.

## Coletas longas

Para buscar todas as cotacoes encontradas, da mais nova para a mais antiga:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=infinito
```

O modo infinito termina depois de 366 tentativas consecutivas sem encontrar
uma data mais antiga. Fontes sem suporte a historico continuam coletando
somente a publicacao atual no fluxo de todas as fontes.

Para expandir gradualmente um historico ja iniciado:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=99
COTACOES_INCREMENTAL_HISTORY=true
```

Se existirem raws historicos ativos, a coleta busca a primeira data antes do
raw mais antigo e mais 99 cotacoes anteriores. Se nao existirem, busca as 100
cotacoes mais recentes. Na CEASA-ES, a data inicial e calculada separadamente
para cada mercado.

Cada raw encontrado e salvo durante o download. Para retomar sem baixar
novamente os raws ativos, use temporariamente:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```
