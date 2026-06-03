# Avaliacao de fontes

Este documento compara as fontes individuais ja usadas no projeto com o arquivo nacional `ProhortDiario.txt` da CONAB.

## Criterios de decisao

### Longevidade

Avalia qual fonte cobre o maior periodo historico util.

Se a fonte individual tiver dados mais antigos que o PROHORT, ela continua importante para historico longo. Se a fonte individual so entregar a cotacao atual, o PROHORT tende a ser melhor para serie historica.

### Detalhes uteis

Avalia se os campos disponiveis atendem ao que o banco realmente usa.

Mais detalhe nao e automaticamente melhor. A fonte individual vale mais quando entrega campos uteis que o PROHORT nao traz, como preco minimo, preco maximo, classificacao, procedencia ou situacao de mercado.

### Atualizacao

Avalia qual fonte publica o dado mais rapido.

Se a fonte individual publica a cotacao no mesmo dia e o PROHORT demora para refletir essa informacao, a fonte individual continua valendo para dados atuais, mesmo que o PROHORT seja melhor para historico padronizado.

### Complementacao

Quando as duas fontes cobrem a mesma CEASA e a mesma data, elas podem ser usadas em conjunto.

Nesse caso, o PROHORT pode servir como base nacional padronizada, enquanto a fonte individual complementa com detalhes extras ou com dados mais recentes. A regra nao deve ser apenas escolher uma fonte e descartar a outra.

## PROHORT Diario

Arquivo avaliado:

- URL: https://portaldeinformacoes.conab.gov.br/downloads/arquivos/ProhortDiario.txt
- Formato: texto separado por `;`
- Tamanho observado: cerca de 174 MB
- Ultima data de cotacao observada: 2026-06-03
- Periodo observado: 2022-06-01 a 2026-06-03
- Cobertura observada: 43 CEASAs, 20 UFs, 48 produtos e unidades `KG`, `UN`, `DZ`

Campos disponiveis:

| Campo | Uso no projeto |
| --- | --- |
| `municipio_ceasa` | cidade da CEASA |
| `cod_ibge_municipio` | codigo IBGE da cidade da CEASA |
| `uf_ceasa` | UF da CEASA |
| `dsc_ceasa` | nome da CEASA |
| `dsc_produto` | produto |
| `sig_unidade_medida` | unidade |
| `data_preco` | data da cotacao |
| `preco_diario` | preco diario, mapeavel para `preco_comum` |

## Matriz inicial

Legenda:

- <span style="color: #22863a;">Forte</span>: vantagem clara para uso.
- <span style="color: #b08800;">Medio</span>: util, mas depende de validacao complementar.
- <span style="color: #d73a49;">Limitado</span>: nao resolve sozinho o criterio.
- <span style="color: #6f42c1;">Combinar</span>: melhor quando usado junto com outra fonte.

Medicoes feitas em 2026-06-02:

- CEASA-PE: calendario publicado no HTML por categoria, com historico geral de 2013-01-02 a 2026-06-02.
- CEASA-PR: pagina antiga publica links por cidade de 2010 a 2021; estrutura unificada medida de 2022-01-03 a 2026-06-02.
- CEASA Campinas: lista paginada de PDFs medida de 2006-10-04 a 2026-06-01. Existe um link com texto interpretado como 1920-01-22, mas o arquivo esta na pasta de 2020; tratar como anomalia da fonte.
- CEASA-MG: o coletor atual nao suporta data alvo; a fonte individual deve ser tratada como ultima cotacao publicada, nao como historico navegavel.

Atualizacao aferida em 2026-06-03:

- PROHORT: 1.021.568 linhas lidas, 43 CEASAs identificadas e data maxima geral em 2026-06-03.
- CEASA-PE: fonte individual com cotacao em 2026-06-03; PROHORT para Recife em 2026-06-02.
- CEASA-MG: fonte individual com Grande BH e Barbacena em 2026-06-03; PROHORT mais recente para unidades CEASAMINAS em 2026-06-01.
- CEASA-PR: fonte individual com Curitiba, Londrina, Foz do Iguacu e Cascavel em 2026-06-03, e Maringa em 2026-06-02; PROHORT com Curitiba e Foz do Iguacu em 2026-06-02, Cascavel em 2026-06-01 e Maringa em 2025-07-29.
- CEASA Campinas: fonte individual com PDF em 2026-06-03; PROHORT para Campinas em 2026-06-01.

| Fonte individual | Coberta no PROHORT? | Historico individual | Historico PROHORT | Detalhes individuais uteis | Atualizacao aferida | Decisao atual |
| --- | --- | --- | --- | --- | --- | --- |
| CEASA-PE | <span style="color: #22863a;">Sim</span>, `CEASA/PE - RECIFE` | <span style="color: #22863a;">Forte</span>: 2013-01-02 a 2026-06-02 no calendario publicado. Por categoria: `organicos` para em 2020-08-06; `flores` comeca em 2014-05-01; demais categorias comecam em 2013-01-02. | <span style="color: #b08800;">Medio</span>: desde 2022-06-01. | <span style="color: #22863a;">Forte</span>: preco minimo, comum, maximo, classificacao, procedencia, situacao e categorias da fonte. | <span style="color: #22863a;">Forte</span>: individual 1 dia mais recente que o PROHORT para Recife na afericao de 2026-06-03. | <span style="color: #6f42c1;">Combinar</span>: CEASA-PE para historico anterior a 2022, dados mais atuais e detalhes; PROHORT para base nacional padronizada. |
| CEASA-MG | <span style="color: #22863a;">Sim</span>, unidades `CEASAMINAS`. | <span style="color: #d73a49;">Limitado</span>: scraper individual coleta apenas a ultima cotacao identificada; nao ha suporte atual a consulta historica por data. | <span style="color: #22863a;">Forte</span>: desde 2022-06-01 para unidades cobertas. | <span style="color: #b08800;">Medio</span>: preco comum por cidade/coluna; poucos detalhes adicionais. | <span style="color: #22863a;">Forte</span>: individual ate 2026-06-03; PROHORT CEASAMINAS ate 2026-06-01. A fonte individual tambem mostrou cidades nao encontradas no PROHORT aferido. | <span style="color: #6f42c1;">Combinar</span>: PROHORT tende a ser principal para historico; manter CEASA-MG para dado mais atual e cidades locais ausentes. |
| CEASA-PR | <span style="color: #22863a;">Sim</span>, Curitiba, Cascavel, Foz do Iguacu e Maringa. | <span style="color: #22863a;">Forte</span>: site publica links antigos por cidade de 2010 a 2021; estrutura unificada medida de 2022-01-03 a 2026-06-02. No scraper atual, a estrutura unificada e suportada a partir de 2022. | <span style="color: #b08800;">Medio</span>: desde 2022-06-01 para unidades cobertas. | <span style="color: #22863a;">Forte</span>: preco minimo, comum, maximo, unidade, classificacao, procedencia e situacao de mercado. | <span style="color: #22863a;">Forte</span>: PDFs individuais mais recentes em todas as unidades comparadas na afericao: Curitiba +1 dia, Foz +1 dia, Cascavel +2 dias e Maringa +308 dias frente ao PROHORT. | <span style="color: #6f42c1;">Combinar</span>: CEASA-PR para historico antigo, atualizacao mais rapida e detalhes; PROHORT para comparacao nacional recente. |
| CEASA Campinas | <span style="color: #22863a;">Sim</span>, `CEASA/SP - CAMPINAS`. | <span style="color: #22863a;">Forte</span>: PDFs medidos de 2006-10-04 a 2026-06-01. Ha uma anomalia de link interpretada como 1920-01-22, mas o arquivo aponta para 2020. | <span style="color: #b08800;">Medio</span>: desde 2022-06-01. | <span style="color: #22863a;">Forte</span>: preco minimo, comum, maximo e grupos de produto extraidos do PDF. | <span style="color: #22863a;">Forte</span>: individual 2 dias mais recente que o PROHORT para Campinas na afericao de 2026-06-03. | <span style="color: #6f42c1;">Combinar</span>: usar as duas se os campos se complementarem; fonte individual agrega historico longo, detalhes e atualizacao mais rapida. |

## Regra pratica

Use o PROHORT como fonte principal quando:

- cobrir a CEASA desejada;
- cobrir o periodo necessario;
- os campos `produto`, `unidade`, `data` e `preco_diario` forem suficientes;
- a atualizacao estiver em prazo aceitavel.

Mantenha ou implemente scraper individual quando:

- a fonte individual tiver historico mais antigo;
- a fonte individual publicar antes do PROHORT;
- a fonte individual trouxer detalhes uteis ausentes no PROHORT;
- a CEASA ou produto nao aparecer no PROHORT;
- for necessario auditar o dado direto na fonte local.

Combine as duas fontes quando:

- ambas cobrirem a mesma CEASA e a mesma data;
- o PROHORT fornecer a base padronizada;
- a fonte individual trouxer campos complementares;
- houver diferenca de atualizacao entre as fontes.

## Proximas medicoes

Para cada CEASA avaliada, registrar:

- menor data historica disponivel na fonte individual;
- campos extras realmente usados pelo banco;
- diferencas de preco entre PROHORT e fonte individual para a mesma data/produto;
- repetir a afericao de atualizacao em outros dias uteis para confirmar se o padrao se mantem;
- decisao final: usar PROHORT, usar fonte individual ou combinar as duas.
