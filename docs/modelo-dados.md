# Modelo de dados

Este documento descreve o modelo de dados vigente do projeto.

## Versao atual

A versao atual usa SQLite com tabelas normalizadas:

- `estados`
- `ceasas`
- `categorias`
- `produtos`
- `unidades`
- `cotacoes`

Arquivo padrao:

- `data/cotacoes.sqlite`

## estados

Representa os estados das fontes coletadas.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| nome | TEXT | Sim | Nome do estado |
| uf | TEXT | Sim | Sigla do estado, unica |

## ceasas

Representa cada fonte/central de abastecimento.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| estado_id | INTEGER | Sim | Referencia para `estados` |
| slug | TEXT | Sim | Identificador da fonte, exemplo: `ceasa-pe` |
| nome | TEXT | Sim | Nome da fonte, exemplo: `CEASA-PE` |
| cidade | TEXT | Nao | Cidade da fonte |
| url_origem | TEXT | Sim | URL base configurada para a fonte |

## categorias

Representa categorias descobertas dentro de uma fonte.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| ceasa_id | INTEGER | Sim | Referencia para `ceasas` |
| slug | TEXT | Sim | Slug da categoria na fonte |
| nome | TEXT | Sim | Nome exibivel da categoria |

A combinacao `ceasa_id + slug` e unica.

## produtos

Representa produtos encontrados nas cotacoes.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| nome_original | TEXT | Sim | Nome como aparece na fonte |
| nome_normalizado | TEXT | Sim | Nome simples para comparacao inicial |

A combinacao `nome_original + nome_normalizado` e unica.

## unidades

Representa as unidades de comercializacao.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| sigla | TEXT | Sim | Unidade como aparece na fonte |
| descricao | TEXT | Nao | Descricao da unidade |

Na versao atual, esta tabela ainda recebe a unidade bruta da fonte. Isso gera muitas variações equivalentes, por exemplo:

- `Kg`, `KG`, `Kg-`
- `Cx.20Kg`, `CX.20Kg`, `Cx.20-kg`, `Cx .20Kg`
- `Frd. 30Kg`, `Frd 30Kg`, `Frd.30Kg`
- `Sc.25 Kg`, `Sc.25Kg`

Tambem existem valores que misturam embalagem e medida:

- `Cx.30 Dz`
- `Cx12Unid.1L`
- `Molho 0,350 Kg`
- `Fardo.5 Kg`
- `50 Espigas`

Por isso, tratar unidade apenas como `sigla` nao e suficiente para analise posterior.

## cotacoes

Representa cada registro de preco coletado.

| Campo | Tipo SQLite | Obrigatorio | Observacao |
| --- | --- | --- | --- |
| id | INTEGER | Sim | Chave primaria autoincremental |
| chave_unica | TEXT | Sim | Hash usado para evitar duplicidade |
| ceasa_id | INTEGER | Sim | Referencia para `ceasas` |
| categoria_id | INTEGER | Sim | Referencia para `categorias` |
| produto_id | INTEGER | Sim | Referencia para `produtos` |
| unidade_id | INTEGER | Nao | Referencia para `unidades` |
| data_cotacao | TEXT | Nao | Data da cotacao em formato ISO `YYYY-MM-DD` |
| preco_minimo | NUMERIC | Nao | Preco minimo sem simbolo de moeda |
| preco_comum | NUMERIC | Nao | Preco comum ou mais frequente |
| preco_maximo | NUMERIC | Nao | Preco maximo |
| procedencia | TEXT | Nao | Procedencia informada pela fonte |
| classificacao | TEXT | Nao | Tipo, classificacao ou variedade |
| situacao_mercado | TEXT | Nao | Situacao do mercado informada pela fonte |
| fonte_complemento | TEXT | Nao | Fonte secundaria usada para preencher campos vazios, quando houver |
| url_complemento | TEXT | Nao | URL da fonte secundaria usada no complemento |
| data_complemento | TEXT | Nao | Data e hora em que o complemento foi aplicado |
| data_coleta | TEXT | Sim | Data e hora em que o scraper salvou o registro |
| url_origem | TEXT | Sim | URL exata usada na coleta |

## Chave unica

A coluna `chave_unica` evita duplicar cotacoes iguais em execucoes repetidas.

Ela considera:

- CEASA.
- Categoria.
- Produto.
- Unidade.
- Procedencia.
- Classificacao.
- Data da cotacao.
- Precos.
- Situacao de mercado.
- URL de origem.

## Banco local

Se existir um banco antigo com a tabela unica `cotacoes`, o scraper interrompe a gravacao e informa que o arquivo deve ser excluido para recriar o schema relacional.

Nao existe backup automatico. A pasta `data/` e ignorada pelo Git e pode ser removida localmente quando for necessario recriar o banco.

## Regras de normalizacao atuais

- Precos sao convertidos para numero decimal.
- Datas sao convertidas para `date` e salvas no SQLite como texto ISO.
- Textos vazios viram `NULL`.
- O nome original do produto e preservado.
- O nome normalizado ainda e simples: minusculo e sem espacos duplicados.
- Unidade, procedencia e classificacao ficam como vierem da fonte.

## Proposta para normalizacao de unidades

A proxima evolucao recomendada e separar o texto bruto da unidade em partes consultaveis:

| Campo proposto | Exemplo | Observacao |
| --- | --- | --- |
| unidade_original | `Cx.20-Kg` | Valor exatamente como veio da fonte |
| unidade_normalizada | `cx 20 kg` | Texto limpo para comparacao |
| embalagem | `caixa` | Tipo de embalagem, quando existir |
| quantidade_minima | `20` | Quantidade, peso ou volume minimo identificado |
| quantidade_maxima | `23` | Usado em faixas como `20a23Kg` |
| unidade_medida | `kg` | Medida base: `kg`, `g`, `l`, `ml`, `un`, `dz`, `cento` |
| detalhe | `tp1` | Sobra util que nao se encaixa nos campos anteriores |

Exemplos esperados:

| Original | Embalagem | Quantidade minima | Quantidade maxima | Unidade medida |
| --- | --- | ---: | ---: | --- |
| `Kg` |  | 1 |  | `kg` |
| `Cx.20Kg` | `caixa` | 20 |  | `kg` |
| `Cx .20a23Kg` | `caixa` | 20 | 23 | `kg` |
| `Cx.30 Dz` | `caixa` | 30 |  | `dz` |
| `Molho 0,350 Kg` | `molho` | 0.350 |  | `kg` |
| `50 Espigas` |  | 50 |  | `espiga` |

O valor original deve continuar salvo para auditoria. Os campos normalizados devem ser derivados por uma funcao de parsing, sem apagar a informacao bruta da fonte.
