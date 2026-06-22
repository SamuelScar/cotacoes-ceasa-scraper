# Comandos

## Comandos principais

Estes sao os comandos usados na operacao normal. Quando estiver trabalhando
com o pacote `data.tar.gz`, prefira chamar os servicos pelo wrapper
`python scripts/cotacoes.py`, porque ele descompacta, executa e recompacta os
dados com seguranca.

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
Se uma fonte falhar depois de baixar parte dos raws, o proprio `tudo` processa
esses arquivos parciais na fase de persistencia e mantem a falha registrada no
relatorio. Se a execucao inteira for interrompida antes da persistencia,
execute `docker compose run --rm salvar` para aproveitar os raws ja salvos.

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

## Pacote de dados

O arquivo versionado e `data.tar.gz`. A pasta `data/` e recriada somente
durante a execucao e removida ao final pelo wrapper operacional.

Use o script abaixo para executar os servicos do Compose mantendo o pacote de
dados seguro:

```bash
python scripts/cotacoes.py tudo
python scripts/cotacoes.py baixar
python scripts/cotacoes.py salvar
python scripts/cotacoes.py app --source ceasa-pe --save
```

O script faz o fluxo completo:

1. cria um lock para impedir duas execucoes simultaneas;
2. descompacta `data.tar.gz` para `data/`, quando necessario;
3. executa o servico Docker solicitado;
4. compacta `data/` em `data.tar.gz.tmp` com `tar` e `pigz`;
5. valida o pacote temporario;
6. substitui `data.tar.gz` somente depois da validacao;
7. remove `data/` ao final.

O `pigz` roda dentro do container e usa multiplas threads para compactar e
descompactar mais rapido. O host precisa apenas de Python, Docker e Docker
Compose.

Se a execucao falhar depois de alterar `data/`, o script ainda tenta gerar um
novo pacote com o estado atual e retorna o codigo de erro do comando original.
Se a compactacao ou validacao falhar, o `data.tar.gz` anterior permanece
preservado e a pasta `data/` fica disponivel para recuperacao.

Para inspecionar manualmente o pacote sem executar o scraper:

```bash
docker compose run --rm --entrypoint tar app -I pigz -tf data.tar.gz
```

O arquivo `data.tar.gz` deve ser rastreado com Git LFS.

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
