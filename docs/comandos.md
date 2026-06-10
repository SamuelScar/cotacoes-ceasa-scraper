# Comandos

## Comandos principais

Estes sao os comandos usados na operacao normal:

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws sem processar |
| `docker compose run --rm salvar` | Processa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Baixa e persiste somente os raws da coleta |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm sincronizar-supabase` | Sincroniza o SQLite com o Supabase |
| `docker compose run --rm compactar-old` | Compacta HTMLs soltos de `old/` |

Data limite, janela historica, caminhos, delay e timeout sao lidos do `.env`.
Uma falha em uma fonte nao interrompe o lote das demais.
Sem `--source`, a CLI executa todas as fontes presentes em `config/fontes.json`.

Com `COTACOES_INCREMENTAL_HISTORY=true`, pedidos de historico continuam antes
do raw ativo mais antigo de cada fonte. A coleta atual com `quotes_back=0`
permanece inalterada.

A saida usa cores em terminais interativos e informa cada raw assim que ele e
salvo. Para desativar as cores, execute o container com `-e NO_COLOR=1`.

Cada comando gera automaticamente um relatorio Markdown proprio em
`data/relatorios/`. O nome identifica o fluxo executado, como
`download_<data_e_hora>.md`, `persistencia_<data_e_hora>.md` ou
`sincronizacao_supabase_<data_e_hora>.md`.

O relatorio registra somente as operacoes pertencentes ao comando solicitado.
Por exemplo, `baixar` registra apenas o download, `salvar` registra somente o
processamento e a persistencia, e `tudo` registra as fases de download e
persistencia separadamente no mesmo arquivo.

Cada relatorio inclui:

- inicio, fim, duracao e status final;
- comando, argumentos, fluxo solicitado e escopo;
- configuracoes efetivamente usadas, sem expor credenciais;
- resumo executivo com resultados consolidados e alertas principais;
- totais de informacoes, acertos, avisos e erros;
- resultados numericos de cada operacao, fonte e fase executada;
- lista completa de avisos e erros;
- historico cronologico de todos os eventos registrados durante a execucao.

O relatorio tambem e salvo quando o comando termina com erro ou e interrompido.
A regra inclui coleta, processamento, manutencao, sincronizacao, consultas e
novos comandos adicionados futuramente.

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

## Diferenca entre os modos

| Comando isolado | Baixa raw | Extrai cotacoes | Salva no SQLite |
| --- | --- | --- | --- |
| `app --source <fonte>` | Sim | Sim | Nao |
| `app --source <fonte> --save` | Sim | Sim | Sim |
| `app --source <fonte> --process-raw` | Nao | Sim | Sim |

O comportamento padrao baixa o raw e valida a extracao sem alterar o banco.
Use `--save` quando quiser concluir o fluxo e persistir as cotacoes.

`--process-raw` nao acessa a fonte. Ele reprocessa os arquivos ativos ja
baixados e salva o resultado.

O fluxo `tudo` processa somente os arquivos selecionados pelo download da
execucao atual. Assim, `quotes_back` tambem limita o volume da persistencia.
Se a execucao for interrompida ainda durante o download, os raws ja baixados
permanecem em disco. Execute `docker compose run --rm salvar` para persisti-los.

## Parametros uteis

| Opcao | Uso |
| --- | --- |
| `--source` | Limita a execucao a uma fonte; sem ele executa todas |
| `--target-date` | Define a data limite |
| `--quotes-back` | Define quantas cotacoes anteriores buscar |
| `--list-categories` | Lista categorias descobertas |
| `--raw-dir` | Sobrescreve o diretorio de raws |
| `--database-path` | Sobrescreve o caminho do SQLite |
| `--http-timeout-seconds` | Sobrescreve o timeout HTTP |
| `--request-delay-seconds` | Sobrescreve o intervalo entre requisicoes |

Os flags `--download-only`, `--download-and-process`, `--archive-raw-old` e
`--complement-prohort` sao usados internamente pelos atalhos definidos no
Compose. Nao e necessario usa-los diretamente.

Para consultar todas as opcoes:

```bash
docker compose run --rm app --help
```

## Raws e reprocessamento

Arquivos ativos ficam em `data/raw/<fonte>/`. Quando outro arquivo do mesmo
grupo e gerado no mesmo dia, a versao anterior vai para `old/`.

O comando `salvar` processa somente arquivos `.html` e `.pdf` diretamente na
pasta da fonte. Ele ignora `old/` e `.zip`.

Use `salvar` quando precisar reprocessar todo o acervo ativo. Na operacao
normal, `tudo` processa somente os raws selecionados na coleta atual.

Para reconstruir o banco depois de uma mudanca de schema ou normalizacao:

```bash
rm data/cotacoes.sqlite
docker compose run --rm salvar
```

O projeto nao migra bancos antigos. Confirme que os raws ativos necessarios
estao presentes antes de excluir o SQLite.

## Complemento PROHORT

Para complementar automaticamente depois de `salvar`, `tudo` ou uma coleta
isolada com `--save`, configure:

```env
COTACOES_COMPLEMENT_PROHORT=true
```

O comando abaixo permanece disponivel para executar somente o complemento sob
demanda:

```bash
docker compose run --rm complementar-prohort
```

Ele:

- preenche `preco_comum` vazio quando encontra correspondencia confiavel;
- nao sobrescreve campos preenchidos;
- pode inserir produtos ausentes para uma CEASA e data ja presentes no banco;
- marca os registros afetados com a origem do complemento.

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
