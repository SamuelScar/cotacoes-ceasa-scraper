# Decisoes tecnicas

Este documento registra somente decisoes vigentes que afetam a manutencao ou a
operacao do projeto.

## Coleta

- Cada fonte possui coletor e parser proprios.
- Categorias, datas e links devem ser descobertos na fonte quando possivel.
- CEASA-MG, CEASA-CE e CEASA-DF coletam somente a publicacao atual.
- `quotes_back` conta datas de cotacao encontradas, nao dias corridos.
- Falhas pontuais de categoria nao interrompem as demais categorias.
- Layouts sem interpretacao confiavel sao rejeitados em vez de persistidos.

## Acesso HTTP

- O cliente aplica delay, jitter, cache por execucao e backoff.
- HTTP 403 e HTTP 429 persistente interrompem a fonte.
- O projeto nao implementa mecanismos para contornar bloqueios.

## Arquivos brutos

- HTMLs e PDFs sao preservados antes do processamento.
- A pasta principal mantem o raw ativo mais recente de cada grupo por dia.
- Versoes anteriores do mesmo grupo vao para `old/`.
- Reprocessamento considera somente raws ativos.
- Compactacao de HTMLs antigos e executada sob demanda.

## Persistencia

- SQLite e o resultado consolidado do projeto.
- O schema separa fontes, entrepostos, produtos, aliases, unidades,
  apresentacoes, coletas e cotacoes.
- O raw e seu hash identificam a proveniencia do registro.
- Identidade comercial e versao observada usam chaves diferentes.
- O banco e recriado a partir dos raws quando o schema muda; nao ha migracoes
  de estruturas antigas.

## Qualidade dos dados

- Nomes originais e apresentacoes comerciais sao preservados.
- Normalizacoes agressivas exigem evidencia de equivalencia.
- Precos negativos sao rejeitados.
- A ordem entre preco minimo, comum e maximo nao e corrigida automaticamente.
- PROHORT complementa o resultado sem sobrescrever campos preenchidos.
- Correspondencias ambiguas do PROHORT nao alteram o banco.
