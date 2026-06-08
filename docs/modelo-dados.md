# Modelo de dados

O banco SQLite separa a origem dos dados, a identidade comercial da cotacao e
os valores observados em cada coleta.

Arquivo padrao:

- `data/cotacoes.sqlite`

## Tabelas

### estados

Representa os estados das fontes coletadas.

| Campo | Descricao |
| --- | --- |
| `id` | Chave primaria |
| `nome` | Nome do estado |
| `uf` | Sigla unica do estado |

### ceasas

Representa a fonte responsavel pela publicacao. Uma CEASA pode possuir varios
entrepostos.

| Campo | Descricao |
| --- | --- |
| `id` | Chave primaria |
| `estado_id` | Estado da fonte |
| `slug` | Identificador unico da fonte |
| `nome` | Nome exibivel |
| `url_origem` | URL base configurada |

### entrepostos

Representa o mercado ou unidade atendida por uma fonte. Cidade nao deve ser
armazenada como categoria ou procedencia.

| Campo | Descricao |
| --- | --- |
| `id` | Chave primaria |
| `ceasa_id` | Fonte responsavel |
| `slug` | Identificador do entreposto dentro da fonte |
| `nome` | Nome como informado pela fonte |

### categorias

Representa grupos de produtos, como `frutas` e `verduras`. Quando a fonte nao
informa um grupo confiavel, usa-se `nao-informada`.

| Campo | Descricao |
| --- | --- |
| `id` | Chave primaria |
| `slug` | Identificador global unico |

### produtos e produto_aliases

`produtos` guarda o nome normalizado usado para comparacao. `produto_aliases`
preserva cada nome original encontrado nas fontes.

| Tabela | Campo principal | Descricao |
| --- | --- | --- |
| `produtos` | `nome_normalizado` | Nome comparavel entre fontes |
| `produto_aliases` | `nome_original` | Nome exatamente como veio da fonte |

A normalizacao de produto ainda e conservadora: minusculas e espacos
duplicados removidos. Equivalencias mais agressivas devem ser criadas somente
quando houver evidencia.

### unidades e apresentacoes_unidade

`unidades` guarda medidas canonicas. `apresentacoes_unidade` preserva a forma
comercial completa encontrada na fonte.

| Campo em `apresentacoes_unidade` | Exemplo |
| --- | --- |
| `unidade_original` | `Cx.20Kg` |
| `unidade_normalizada` | `caixa 20 kg` |
| `embalagem` | `caixa` |
| `quantidade_minima` | `20` |
| `quantidade_maxima` | `23` em uma faixa |
| `detalhe_unidade` | Texto restante util |

Apresentacoes iguais sao reutilizadas entre cotacoes. Valores nao reconhecidos
continuam preservados mesmo sem unidade canonica.

### coletas

Registra a proveniencia de cada processamento.

| Campo | Descricao |
| --- | --- |
| `chave_unica` | Identidade tecnica da coleta |
| `ceasa_id` | Fonte processada |
| `arquivo_raw` | Caminho do arquivo bruto, quando existir |
| `hash_raw` | SHA-256 do conteudo bruto |
| `url_origem` | URL representada pela coleta |
| `baixado_em` | Data extraida do nome do raw |
| `processado_em` | Momento do processamento |

Ao reconstruir o banco pelos arquivos ativos, cotacoes do mesmo raw apontam
para a mesma coleta. Reprocessar o mesmo arquivo nao cria outra coleta.

### cotacoes

Representa os valores observados para uma identidade comercial em uma coleta.

| Campo | Obrigatorio | Descricao |
| --- | --- | --- |
| `chave_unica` | Sim | Identifica uma versao observada |
| `chave_identidade` | Sim | Identifica a cotacao sem considerar coleta e precos |
| `coleta_id` | Sim | Proveniencia do registro |
| `entreposto_id` | Nao | Mercado ao qual o preco pertence |
| `categoria_id` | Sim | Grupo de produtos |
| `produto_alias_id` | Sim | Nome original ligado ao produto normalizado |
| `apresentacao_unidade_id` | Nao | Unidade e embalagem informadas |
| `data_cotacao` | Sim | Data ISO `YYYY-MM-DD` |
| `preco_minimo` | Nao | Preco minimo nao negativo |
| `preco_comum` | Nao | Preco comum nao negativo |
| `preco_maximo` | Nao | Preco maximo nao negativo |
| `procedencia` | Nao | Origem do produto |
| `classificacao` | Nao | Tipo, variedade ou classificacao |
| `situacao_mercado` | Nao | Situacao informada pela fonte |
| `fonte_complemento` | Nao | Fonte secundaria utilizada |
| `url_complemento` | Nao | URL da fonte secundaria |
| `data_complemento` | Nao | Momento do complemento |

## Identidade e versoes

`chave_identidade` considera fonte, entreposto, categoria, produto
normalizado, apresentacao, procedencia, classificacao e data da cotacao.

`chave_unica` acrescenta a coleta, os precos e a situacao de mercado. Assim:

- Reprocessar o mesmo raw nao duplica registros.
- Publicacoes diferentes para a mesma identidade permanecem auditaveis.
- Cotacoes semelhantes de entrepostos diferentes nao sao misturadas.

## Integridade

- Datas ausentes e cotacoes sem nenhum preco nao sao persistidas.
- Precos negativos sao rejeitados.
- A ordem entre minimo, comum e maximo nao e alterada automaticamente.
- Chaves estrangeiras sao ativadas durante a gravacao.
- Indices atendem consultas por coleta, entreposto/data, categoria/data,
  produto/data e identidade.
- IDs usam `INTEGER PRIMARY KEY`, sem `AUTOINCREMENT`.
- Insercoes tratam somente conflitos esperados por chave unica.

Nao existe conversao de bancos antigos. Quando o schema mudar, o arquivo
SQLite deve ser excluido e reconstruido a partir dos arquivos brutos ativos.
