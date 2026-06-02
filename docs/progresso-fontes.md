# Progresso das fontes

Este documento acompanha o avanco dos scrapers por fonte.

## Resumo

| Fonte | UF | Scraper atual | Scraper anteriores | Observacao |
| --- | --- | --- | --- | --- |
| CEASA-PE | PE | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta todas as categorias descobertas e permite buscar datas anteriores com `COTACOES_QUOTES_BACK`. |
| CEASA-MG | MG | <span style="color: #22863a;">Concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Coleta a ultima cotacao por cidade; nao foi identificado acesso confiavel a cotacoes anteriores. |
| CEASA-PR | PR | <span style="color: #22863a;">Concluido</span> | <span style="color: #22863a;">Concluido</span> | Coleta PDFs diarios por cidade na estrutura unificada a partir de 2022. |
| CEAGESP-SP | SP | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Pode exigir formulario por categoria, produto ou data. |
| CEASA-RJ | RJ | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Precisa reavaliar URL e estrutura da pagina. |
| CEASA-DF | DF | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Precisa avaliar estrutura da pagina de precos. |
| CEASA Campinas | SP | <span style="color: #b08800;">Parcial</span> | <span style="color: #b08800;">Parcial</span> | Scraper implementado para PDFs descobertos dinamicamente; pendente de validacao local. |
| CEASA-GO | GO | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Precisa avaliar se entrega HTML direto ou arquivo. |
| CEASA-BA | BA | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Baixa prioridade por aparentar boletim ou pagina institucional. |
| CEASA-CE | CE | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Precisa avaliar HTML ou boletim diario. |
| CEASA-ES | ES | <span style="color: #d73a49;">Nao concluido</span> | <span style="color: #d73a49;">Nao concluido</span> | Precisa avaliar HTML ou arquivo. |

## Legenda

- <span style="color: #22863a;">Concluido</span>: scraper implementado para o fluxo indicado.
- <span style="color: #b08800;">Parcial</span>: fonte avaliada ou em implementacao, mas scraper ainda nao concluido.
- <span style="color: #d73a49;">Nao concluido</span>: fonte ainda nao analisada, nao implementada ou sem caminho confiavel para esse tipo de coleta.

## Criterios

### Scraper atual

Coleta a cotacao mais recente disponivel na fonte.

### Scraper anteriores

Coleta cotacoes historicas ou datas anteriores por parametro, arquivo oficial estavel ou pagina oficial de historico.
