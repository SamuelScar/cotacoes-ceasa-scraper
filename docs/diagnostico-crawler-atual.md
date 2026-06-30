# Diagnostico do crawler atual

Registro criado antes de substituir o pacote `data/` por uma nova copia baixada
do workflow.

## Estado local observado

- Banco local: `data/cotacoes.sqlite`
- Tamanho do banco: `356061184` bytes (`340M` no `ls -lh`)
- Modificacao do banco no filesystem: `2026-06-10 13:42`
- Relatorio de coleta mais recente relevante:
  `data/relatorios/coleta_20260610_151435_605438.md`
- Relatorios de erro `execucao_20260630_122002_*.md` foram gerados por uma
  tentativa local de teste com o entrypoint errado da CLI e nao representam
  rodada real do crawler.

## Banco SQLite atual

| Tabela | Registros |
| --- | ---: |
| estados | 10 |
| ceasas | 11 |
| entrepostos | 24 |
| categorias | 35 |
| produtos | 1455 |
| produto_aliases | 1551 |
| unidades | 11 |
| apresentacoes_unidade | 596 |
| coletas | 10459 |
| cotacoes | 780894 |

Periodos observados:

- `coletas.processado_em`: de `2026-06-10T15:22:26` a
  `2026-06-10T16:40:45`.
- `cotacoes.data_cotacao`: de `2010-09-01` a `2026-06-09`.

Distribuicao por fonte no banco:

| Fonte | Coletas | Cotacoes |
| --- | ---: | ---: |
| ceagesp-sp | 28 | 2404 |
| ceasa-ba | 73 | 6598 |
| ceasa-campinas | 104 | 31783 |
| ceasa-ce | 27 | 1628 |
| ceasa-df | 3 | 267 |
| ceasa-es | 2059 | 216431 |
| ceasa-go | 200 | 47397 |
| ceasa-mg | 3 | 755 |
| ceasa-pe | 7024 | 251496 |
| ceasa-pr | 596 | 153138 |
| ceasa-rj | 342 | 68997 |

## Relatorio principal preservado

Arquivo: `data/relatorios/coleta_20260610_151435_605438.md`

Resumo executivo:

- Inicio: `2026-06-10T15:14:35+00:00`
- Fim: `2026-06-10T16:42:32+00:00`
- Duracao: `5277.11 segundos`
- Status: **Concluida com avisos**
- Avisos: `7`
- Erros: `0`
- Cotacoes processadas: `781066`
- Registros novos: `780894`
- Fontes concluidas: `11`
- Fontes com falha: `0`
- Fontes ignoradas: `0`

Resultados por fonte no relatorio:

| Fonte | Cotacoes processadas | Registros novos |
| --- | ---: | ---: |
| CEASA-PE | 251496 | 251496 |
| CEASA-MG | 755 | 755 |
| CEASA-PR | 153301 | 153138 |
| CEASA Campinas | 31783 | 31783 |
| CEASA-GO | 47399 | 47397 |
| CEASA-CE | 1628 | 1628 |
| CEASA-RJ | 68997 | 68997 |
| CEASA-BA | 6598 | 6598 |
| CEASA-DF | 273 | 267 |
| CEAGESP-SP | 2404 | 2404 |
| CEASA-ES | 216432 | 216431 |

Alertas principais:

- `3` ocorrencias de PDF invalido: `Invalid Elementary Object`.
- `3` ocorrencias de `Stream has ended unexpectedly`.
- `1` ocorrencia de layout antigo da CEASA Campinas sem suporte confiavel.

## Sinal de possivel reinicio do banco

O relatorio `coleta_20260610_151435_605438.md` foi um processamento de raws
ativos sem acesso HTTP e registrou `780894` registros novos para `781066`
cotacoes processadas. No banco atual, todos os registros de `coletas` tem
`processado_em` dentro da janela dessa execucao.

Isso indica que o SQLite local provavelmente estava vazio, ausente ou foi
recriado antes desse processamento. A proxima analise deve comparar esse estado
com o pacote que sera baixado do workflow para descobrir se o asset publicado
perdeu o banco anterior ou se o reset aconteceu antes da compactacao.
