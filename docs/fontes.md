# Fontes de dados

Este documento registra as fontes previstas para coleta, o tipo esperado de acesso e o status de avaliacao.

Para comparar fontes individuais com o PROHORT/CONAB, ver [Avaliacao de fontes](avaliacao-fontes.md).

## Criterios de prioridade

As primeiras fontes devem atender ao maior numero possivel destes criterios:

- Dados disponiveis em HTML ou API publica.
- Estrutura de pagina estavel.
- Tabela simples de precos.
- Baixo risco de bloqueio.
- Dados com data de cotacao clara.

Fontes em PDF, arquivos baixados manualmente, paginas muito dinamicas ou com captcha devem ficar fora do escopo inicial.

## Fontes previstas

| Fonte | UF | URL | Tipo esperado | Prioridade | Status |
| --- | --- | --- | --- | --- | --- |
| CEASA-PE | PE | https://www.ceasape.org.br/cotacao | HTML | Alta | Implementado |
| CEASA-MG | MG | https://minas1.ceasa.mg.gov.br/ceasainternet/cst_precosmaiscomumMG/cst_precosmaiscomumMG.php | HTML | Alta | Implementado para ultima cotacao |
| CEASA-PR | PR | https://www.ceasa.pr.gov.br/Pagina/Cotacao-Diaria-de-Precos | HTML + PDF | Media | Implementado para estrutura unificada desde 2022 |
| CEAGESP-SP | SP | https://ceagesp.gov.br/cotacoes/ | HTML | Media | Pendente |
| CEASA-RJ | RJ | https://www.rj.gov.br/ceasa/Cota%C3%A7%C3%A3o | HTML + PDF | Alta | Implementado e validado para PDFs diarios |
| CEASA-DF | DF | https://www.portal.ceasadf.com.br/precos | HTML | Media | Pendente |
| CEASA Campinas | SP | https://www.ceasacampinas.com.br/cotacoes-anteriores | HTML + PDF | Media | Implementado, pendente de validacao |
| CEASA-GO | GO | https://goias.gov.br/ceasa/cotacoes-diarias/ | HTML + PDF | Media | Implementado para PDFs diarios, pendente de validacao |
| CEASA-BA | BA | https://www.ba.gov.br/sde/boletim-informativo-ceasa | Arquivo ou pagina institucional | Baixa | Pendente |
| CEASA-CE | CE | https://files.ceasa-ce.com.br/unsima/boletim_diario/boletim.php | HTML + PDF | Media | Implementado para boletins atuais |
| CEASA-ES | ES | https://ceasa.es.gov.br/boletim | HTML ou arquivo | Media | Pendente |

## Campos para avaliacao de cada fonte

Ao analisar uma fonte, registrar:

- URL final usada pelo scraper.
- Metodo de acesso: HTML, API, CSV, XLSX, PDF ou outro.
- Se exige parametros de data, produto ou mercado.
- Se existe paginacao.
- Se os valores usam virgula decimal.
- Se a data da cotacao aparece na pagina.
- Se a fonte informa unidade, procedencia, classificacao ou situacao de mercado.
- Riscos conhecidos.

## Decisao inicial

A primeira implementacao deve usar uma fonte HTML simples. A escolha final deve ser feita depois de inspecionar as paginas, mas CEASA-MG e CEASA-PE sao boas candidatas para iniciar.
