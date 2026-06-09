# Fontes e limitacoes

As fontes configuradas ficam em `config/fontes.json`. A tabela abaixo registra
somente diferencas que afetam a operacao.

| Slug | Formato | Historico | Limitacao principal |
| --- | --- | --- | --- |
| `ceasa-pe` | HTML | Sim | Categorias e datas sao descobertas na pagina |
| `ceasa-mg` | HTML | Nao | Coleta somente a ultima cotacao por entreposto |
| `ceasa-pr` | HTML + PDF | Sim | Estrutura suportada a partir de 2022 |
| `ceasa-campinas` | HTML + PDF | Sim | Layouts antigos sem data confiavel sao rejeitados |
| `ceasa-go` | HTML + PDF | Sim | Navega por ano, mes e PDF diario |
| `ceasa-ce` | HTML + PDF | Nao | Coleta somente os boletins atuais |
| `ceasa-rj` | HTML + PDF | Sim | Navega por ano, mes e PDF diario |
| `ceasa-ba` | HTML + PDF | Sim | Usa a lista historica de boletins |
| `ceasa-df` | HTML + PDF | Nao | Coleta somente o boletim SIMA atual |
| `ceagesp-sp` | HTML | Sim, limitado | A pagina expoe apenas uma janela recente |
| `ceasa-es` | HTML | Sim | Cada mercado possui datas independentes |

`--quotes-back` e `COTACOES_QUOTES_BACK` sao aplicados somente nas fontes com
historico. Nos comandos que percorrem todas as fontes, a janela e zerada
automaticamente para CEASA-MG, CEASA-CE e CEASA-DF.

O valor `infinito` busca datas da mais nova para a mais antiga e encerra depois
de 366 tentativas consecutivas sem encontrar uma cotacao anterior.

Com `COTACOES_INCREMENTAL_HISTORY=true`, fontes com historico iniciam antes do
raw ativo mais antigo quando `COTACOES_TARGET_DATE` esta vazio. A CEASA-ES usa
o raw mais antigo de cada mercado porque suas datas sao independentes.

## PROHORT

O `ProhortDiario.txt` da CONAB e usado somente como complemento. Os scrapers
individuais permanecem como origem principal porque podem fornecer historico
mais longo, dados mais recentes ou campos ausentes no PROHORT.

O complemento compara CEASA, data, produto e unidade. Correspondencias
ambiguas ou sem mapeamento confiavel nao alteram registros existentes.
A URL fica em `config/prohort.json` e `COTACOES_COMPLEMENT_PROHORT` decide se o
complemento roda automaticamente depois da persistencia.

## Adicionar uma fonte

1. Adicionar os metadados em `config/fontes.json`.
2. Criar coletor e parser especificos.
3. Registrar a fonte em `sources/registry.py`.
4. Reutilizar `HttpClient`, normalizadores e storages existentes.
5. Registrar nesta tabela apenas limitacoes relevantes para operacao.
