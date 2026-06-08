# Modelo de dados

O arquivo padrao e `data/cotacoes.sqlite`. O schema separa origem, proveniencia,
identidade comercial e versoes observadas.

## Tabelas

| Tabela | Responsabilidade |
| --- | --- |
| `estados` | UF e nome do estado |
| `ceasas` | Fonte responsavel pela publicacao |
| `entrepostos` | Mercado ou unidade atendida pela fonte |
| `categorias` | Grupo de produtos |
| `produtos` | Nome normalizado para comparacao |
| `produto_aliases` | Nome original encontrado na fonte |
| `unidades` | Medida canonica |
| `apresentacoes_unidade` | Embalagem, quantidade e texto original |
| `coletas` | Proveniencia de cada raw processado |
| `cotacoes` | Valores observados em uma coleta |

## Proveniencia

`coletas` registra:

- fonte;
- caminho e hash SHA-256 do raw;
- URL de origem;
- data de download;
- momento do processamento.

Reprocessar o mesmo raw reutiliza a coleta existente.

## Produtos e unidades

`produtos.nome_normalizado` e usado para comparacao conservadora.
`produto_aliases.nome_original` preserva o texto da fonte.

`apresentacoes_unidade` preserva `unidade_original` e deriva:

- `unidade_normalizada`;
- `embalagem`;
- `quantidade_minima`;
- `quantidade_maxima`;
- `detalhe_unidade`;
- referencia opcional para uma unidade canonica.

Valores nao reconhecidos continuam preservados.

## Identidade e versoes

`cotacoes.chave_identidade` identifica a cotacao sem considerar coleta, precos
ou situacao de mercado. Ela considera fonte, entreposto, categoria, produto,
apresentacao, procedencia, classificacao e data.

`cotacoes.chave_unica` acrescenta coleta, precos e situacao. Com isso:

- reprocessar o mesmo raw nao duplica registros;
- publicacoes diferentes permanecem auditaveis;
- entrepostos diferentes nao sao misturados.

## Integridade

- Cotacoes sem data ou sem nenhum preco nao sao persistidas.
- Precos negativos sao rejeitados.
- A ordem entre minimo, comum e maximo nao e corrigida automaticamente.
- Chaves estrangeiras sao ativadas durante a gravacao.
- O banco antigo nao e migrado.

Quando o schema mudar, exclua o SQLite e reconstrua a base usando os raws
ativos.
