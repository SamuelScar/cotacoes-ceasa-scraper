# Comandos

## Comandos principais

Estes sao os comandos usados na operacao normal:

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws sem processar |
| `docker compose run --rm salvar` | Processa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Executa download e persistencia |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm compactar-old` | Compacta HTMLs soltos de `old/` |

Data limite, janela historica, caminhos, delay e timeout sao lidos do `.env`.
Uma falha em uma fonte nao interrompe o lote das demais.

A saida usa cores em terminais interativos e informa cada raw assim que ele e
salvo. Para desativar as cores, execute o container com `-e NO_COLOR=1`.

## Executar uma fonte

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

## Parametros uteis

| Opcao | Uso |
| --- | --- |
| `--source` | Seleciona uma fonte |
| `--target-date` | Define a data limite |
| `--quotes-back` | Define quantas cotacoes anteriores buscar |
| `--list-categories` | Lista categorias descobertas |
| `--raw-dir` | Sobrescreve o diretorio de raws |
| `--database-path` | Sobrescreve o caminho do SQLite |
| `--http-timeout-seconds` | Sobrescreve o timeout HTTP |
| `--request-delay-seconds` | Sobrescreve o intervalo entre requisicoes |

Os flags `--all-sources`, `--download-only`, `--download-and-process`,
`--archive-raw-old` e `--complement-prohort` sao usados internamente pelos
atalhos definidos no Compose. Nao e necessario usa-los diretamente.

Para consultar todas as opcoes:

```bash
docker compose run --rm app --help
```

## Raws e reprocessamento

Arquivos ativos ficam em `data/raw/<fonte>/`. Quando outro arquivo do mesmo
grupo e gerado no mesmo dia, a versao anterior vai para `old/`.

O comando `salvar` processa somente arquivos `.html` e `.pdf` diretamente na
pasta da fonte. Ele ignora `old/` e `.zip`.

Para reconstruir o banco depois de uma mudanca de schema ou normalizacao:

```bash
rm data/cotacoes.sqlite
docker compose run --rm salvar
```

O projeto nao migra bancos antigos. Confirme que os raws ativos necessarios
estao presentes antes de excluir o SQLite.

## Complemento PROHORT

```bash
docker compose run --rm complementar-prohort
```

O complemento deve ser executado depois dos scrapers individuais. Ele:

- preenche `preco_comum` vazio quando encontra correspondencia confiavel;
- nao sobrescreve campos preenchidos;
- pode inserir produtos ausentes para uma CEASA e data ja presentes no banco;
- marca os registros afetados com a origem do complemento.

## Coletas longas

Para solicitar as 100 ultimas cotacoes disponiveis:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=99
```

Cada raw encontrado e salvo durante o download. Para retomar sem baixar
novamente os raws ativos, use temporariamente:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```
