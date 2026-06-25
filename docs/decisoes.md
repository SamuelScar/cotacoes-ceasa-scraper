# Decisoes tecnicas

Este documento registra somente decisoes vigentes que afetam a manutencao ou a
operacao do projeto.

## Coleta

- Cada fonte possui coletor e parser proprios.
- Categorias, datas e links devem ser descobertos na fonte quando possivel.
- CEASA-MG, CEASA-CE e CEASA-DF coletam somente a publicacao atual.
- `quotes_back` conta datas de cotacao encontradas, nao dias corridos.
- `quotes_back=infinito` encerra apos 366 tentativas consecutivas sem uma data
  mais antiga, evitando execucao sem fim em fontes com historico esgotado.
- O modo historico incremental continua antes do raw ativo mais antigo quando
  `quotes_back` solicita historico e nenhuma data limite manual foi informada.
- Uma continuacao incremental sem novas datas encerra normalmente quando o
  historico ja estiver esgotado.
- `quotes_back=0` sempre preserva a coleta da publicacao atual, mesmo com o modo
  incremental ativo.
- Falhas pontuais de categoria nao interrompem as demais categorias.
- Nos fluxos de todas as fontes, o download pode executar fontes diferentes em
  paralelo, mas cada fonte continua sequencial internamente.
- Layouts sem interpretacao confiavel sao rejeitados em vez de persistidos.

## Crawler

- A execucao continua atual e feita por GitHub Actions, usando
  `.github/workflows/scraper-release.yml`.
- O estado entre rodadas fica no asset `ceasa-data-latest.tar.gz` da release
  fixa `latest-data`.
- Cada rodada restaura `data/`, executa `docker compose run --rm tudo`, compacta
  a pasta e publica novamente o asset.
- Um servico local `crawler` permanente nao faz parte da operacao atual.

## Acesso HTTP

- O cliente aplica delay, jitter, cache por execucao e backoff.
- HTTP 403, HTTP 429 e falhas de conexao persistentes interrompem a fonte.
- O projeto nao implementa mecanismos para contornar bloqueios.

## Arquivos brutos

- HTMLs e PDFs sao preservados antes do processamento.
- A pasta principal mantem o raw ativo mais recente de cada grupo por dia.
- Versoes anteriores do mesmo grupo vao para `old/`.
- `tudo` processa somente os raws selecionados na coleta atual.
- `salvar` reprocessa todos os raws ativos e ignora `old/` e `.zip`.
- Compactacao de HTMLs antigos e executada sob demanda.

## Persistencia

- SQLite e o resultado consolidado do projeto.
- O schema separa fontes, entrepostos, produtos, aliases, unidades,
  apresentacoes, coletas e cotacoes.
- O raw e seu hash identificam a proveniencia do registro.
- Identidade comercial e versao observada usam chaves diferentes.
- A persistencia SQLite permanece sequencial, mesmo quando o download roda com
  mais de um worker.
- O banco e recriado a partir dos raws quando o schema muda; nao ha migracoes
  de estruturas antigas.

## Qualidade dos dados

- Nomes originais e apresentacoes comerciais sao preservados.
- Normalizacoes agressivas exigem evidencia de equivalencia.
- Precos negativos sao rejeitados.
- A ordem entre preco minimo, comum e maximo nao e corrigida automaticamente.
- PROHORT complementa o resultado sem sobrescrever campos preenchidos.
- Correspondencias ambiguas do PROHORT nao alteram o banco.
- A URL do PROHORT fica versionada em `config/prohort.json`.
- `COTACOES_COMPLEMENT_PROHORT` controla a execucao automatica depois de
  qualquer fluxo que salva no SQLite.
