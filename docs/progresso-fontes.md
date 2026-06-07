# Progresso das fontes

Este documento acompanha o avanco dos scrapers por fonte.

## Resumo

| Fonte | UF | Scraper atual | Scraper anteriores | Observacao |
| --- | --- | --- | --- | --- |
| CEASA-PE | PE | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta todas as categorias descobertas e permite buscar datas anteriores com `COTACOES_QUOTES_BACK`. |
| CEASA-MG | MG | <span style="color: #22863a;">Concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Coleta a ultima cotacao por cidade; nao foi identificado acesso confiavel a cotacoes anteriores. |
| CEASA-PR | PR | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta PDFs diarios por cidade na estrutura unificada a partir de 2022. |
| CEAGESP-SP | SP | <span style="color: #22863a;">Concluido</span> | <span style="color: #b08800;">Parcial</span> | Coleta o formulario da capital por categoria e data. O fluxo atual e `--quotes-back` foram validados em 2026-06-06, mas a pagina expoe apenas uma janela recente de datas. |
| CEASA-RJ | RJ | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta PDFs diarios navegados por ano e mes; fluxo atual e `--quotes-back` validados em 2026-06-06. |
| CEASA-DF | DF | <span style="color: #22863a;">Concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Coleta o boletim SIMA atual; fluxo validado com 91 cotacoes em 2026-06-06. A pagina oficial nao expoe lista historica direta. |
| CEASA Campinas | SP | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Fluxo atual, `--quotes-back` e navegacao para 2025 validados em 2026-06-07. |
| CEASA-GO | GO | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Fluxo atual, `--quotes-back` e navegacao para 2025 validados em 2026-06-07. |
| CEASA-BA | BA | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta PDFs diarios listados na pagina oficial; fluxo atual, `--quotes-back` e navegacao para 2025 validados em 2026-06-06. |
| CEASA-CE | CE | <span style="color: #22863a;">Concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Fluxo atual validado com 544 cotacoes em 2026-06-07; nao foi implementado historico por data. |
| CEASA-ES | ES | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Os tres mercados e suas datas independentes foram validados em 2026-06-07; Noroeste publica ate 2024-12-20 e Cachoeiro ate 2018-08-27. |

## Legenda

- <span style="color: #22863a;">Concluido</span>: scraper implementado para o fluxo indicado.
- <span style="color: #b08800;">Parcial</span>: fonte avaliada ou em implementacao, mas scraper ainda nao concluido.
- <span style="color: #d73a49;">Nao concluido</span>: fonte ainda nao analisada, nao implementada ou sem caminho confiavel para esse tipo de coleta.

## Criterios

### Scraper atual

Coleta a cotacao mais recente disponivel na fonte.

### Scraper anteriores

Coleta cotacoes historicas ou datas anteriores por parametro, arquivo oficial estavel ou pagina oficial de historico.
