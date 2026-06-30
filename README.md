# Cotacoes CEASA Scraper

Coleta cotacoes publicas de CEASAs brasileiras, preserva os arquivos brutos e
consolida os registros em um banco SQLite normalizado.

## Fluxo do projeto

1. Descobrir categorias e datas disponiveis em cada fonte.
2. Baixar HTMLs ou PDFs para `data/raw/<fonte>/`.
3. Processar os arquivos brutos com o parser da fonte.
4. Salvar cotacoes, proveniencia e versoes em `data/cotacoes.sqlite`.
5. Opcionalmente complementar campos vazios com o PROHORT.
6. No crawler atual, restaurar `data/` da release fixa, executar uma nova rodada
   e republicar o pacote atualizado.

Os coletores individuais continuam sendo a fonte principal. O PROHORT nao
sobrescreve valores ja preenchidos. Sua URL fica versionada em
`config/prohort.json`.

## Inicio rapido

Requisitos: Docker e Docker Compose.

```bash
cp .env.example .env
docker compose build
docker compose run --rm tudo
```

O servico `tudo` baixa os arquivos brutos de todas as fontes configuradas,
processa somente os raws selecionados nessa coleta e salva o resultado no SQLite. Com
`COTACOES_COMPLEMENT_PROHORT=true`, ele tambem executa o complemento PROHORT ao
final.

Por padrao, a CLI executa todas as fontes presentes em `config/fontes.json`.
Use `--source <fonte>` somente quando quiser executar uma fonte especifica.
Nos fluxos de todas as fontes, `COTACOES_WORKERS` ou `--workers` controlam
quantas fontes sao baixadas em paralelo. O padrao `1` preserva a execucao
sequencial.

Comandos principais:

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws de todas as fontes |
| `docker compose run --rm salvar` | Reprocessa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Baixa, processa os raws da coleta e salva |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm sincronizar-supabase` | Adiciona novos registros ao Supabase |
| `docker compose run --rm substituir-supabase` | Substitui completamente o Supabase |
| `docker compose run --rm migrar-supabase-pgloader` | Executa migracao completa excepcional |
| `docker compose run --rm compactar-old` | Compacta HTMLs antigos |
| `docker compose run --rm app --help` | Exibe todas as opcoes da CLI |

Exemplo de download paralelo pontual:

```bash
docker compose run --rm app --download-only --workers 3
docker compose run --rm app --download-and-process --workers 3
```

Para usar os atalhos `baixar` e `tudo`, configure `COTACOES_WORKERS=3` no
`.env` e execute os comandos normais.

## Crawler atual

Por enquanto, a execucao continua do projeto e feita pelo GitHub Actions, nao por
um processo Python permanente.

O workflow `.github/workflows/scraper-release.yml` roda em horarios agendados e
tambem pode ser disparado manualmente. A cada execucao valida, ele:

1. restaura o pacote completo do OneDrive quando configurado, ou
   `ceasa-data-latest.tar.gz` da release `latest-data` como fallback;
2. executa `docker compose run --rm tudo`;
3. mesmo se o scraper terminar com erro, tenta compactar e salvar os dados;
4. salva o pacote completo com SQLite no OneDrive quando configurado;
5. publica o pacote enxuto sem SQLite como `ceasa-data-latest.tar.gz`;
6. valida se ao menos um pacote foi salvo fora do runner;
7. anexa ao relatorio o status de restauracao, backup e publicacao;
8. envia o ultimo relatorio por e-mail quando os secrets SMTP estao configurados.

O pacote da release preserva raws, cache e relatorios, mas nao inclui
`data/cotacoes.sqlite`. O pacote do OneDrive inclui o SQLite.
Se o OneDrive ainda nao estiver configurado, o workflow segue pelo fluxo da
release do GitHub e nao bloqueia a coleta.

Esse fluxo e o crawler oficial do projeto neste momento. Um servico local
`docker compose up crawler` fica reservado para uma evolucao futura, caso sejam
necessarios intervalos por fonte, backoff persistente ou observabilidade de
processo continuo.

## Janela de coleta

`COTACOES_TARGET_DATE` define a data limite. Quando fica vazio, cada fonte busca
a ultima cotacao disponivel.

`COTACOES_QUOTES_BACK` informa quantas datas de cotacao anteriores devem ser
coletadas. O valor nao representa dias corridos e tambem aceita `infinito`:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=infinito
```

Essa configuracao busca todas as datas encontradas, da mais nova para a mais
antiga, nas fontes que oferecem historico. A busca termina depois de 366
tentativas consecutivas sem encontrar uma data mais antiga. CEASA-MG, CEASA-CE
e CEASA-DF coletam somente a publicacao atual.

Com `COTACOES_INCREMENTAL_HISTORY=true`, pedidos de historico continuam antes
do raw ativo mais antigo. `COTACOES_TARGET_DATE` manual tem prioridade e
`COTACOES_QUOTES_BACK=0` continua buscando a publicacao atual.

## Arquivos gerados

- `data/raw/<fonte>/`: raws ativos usados no reprocessamento.
- `data/raw/<fonte>/old/`: versoes anteriores geradas no mesmo dia.
- `data/cotacoes.sqlite`: banco consolidado local, regeneravel a partir dos raws.
- `data/relatorios/<fluxo>_<data_e_hora>.md`: relatorio completo de cada comando.

O processamento ignora `old/` e arquivos `.zip`. Quando o schema ou uma regra
de normalizacao mudar, exclua o SQLite e reconstrua o banco a partir dos raws
ativos.

Cada relatorio apresenta primeiro o comando solicitado, o escopo, um resumo
executivo, resultados consolidados, alertas principais e as configuracoes
efetivamente usadas sem credenciais. Em seguida, registra somente as operacoes
realizadas pelo comando, com resultados por fonte e fase, avisos, erros e o
historico cronologico completo. O arquivo tambem e gerado quando a execucao
termina com erro ou e interrompida.

## Documentacao

- [Ambiente e configuracao](docs/ambiente.md)
- [Comandos](docs/comandos.md)
- [Fontes e limitacoes](docs/fontes.md)
- [Modelo de dados](docs/modelo-dados.md)
- [Sincronizacao com Supabase](docs/supabase.md)
- [Estrategias contra bloqueio](docs/estrategias-anti-bloqueio.md)
- [Decisoes tecnicas](docs/decisoes.md)
- [Pendencias](docs/pendencias.md)
