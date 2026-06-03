# Plano de implementacao

Plano inicial para transformar a proposta em um scraper funcional.

## Etapa 1: Base documental

Objetivo: deixar claro o que sera coletado, de onde vira e como os dados serao armazenados.

Tarefas:

- Registrar fontes previstas.
- Definir modelo de dados inicial.
- Definir ordem de implementacao.
- Documentar decisoes tecnicas.

Status: em andamento.

## Etapa 2: Estrutura do projeto

Objetivo: criar uma base simples para desenvolvimento em Python.

Estrutura sugerida:

```text
cotacoes-ceasa-scraper/
|-- docs/
|-- src/
|   `-- cotacoes_ceasa/
|       |-- collectors/
|       |-- normalizers/
|       |-- storage/
|       `-- main.py
|-- data/
|   |-- raw/
|   `-- processed/
|-- README.md
`-- pyproject.toml
```

Responsabilidades:

- `collectors`: acessam fontes externas e extraem dados brutos.
- `normalizers`: convertem dados brutos para o formato padrao.
- `storage`: salva dados em SQLite.
- `main.py`: ponto de entrada para executar coletas.

## Etapa 3: Primeira fonte

Objetivo: implementar uma fonte de ponta a ponta.

Fluxo esperado:

1. Acessar a pagina da fonte.
2. Salvar o HTML bruto.
3. Extrair tabela de cotacoes.
4. Converter os dados para o formato padrao.
5. Salvar em SQLite.
6. Documentar limitacoes da fonte.

Fonte inicial recomendada:

- CEASA-PE.

Status: coleta de HTML bruto implementada para CEASA-PE.

## Etapa 4: Persistencia inicial

Objetivo: salvar os dados em SQLite sem complexidade desnecessaria.

Ordem recomendada:

1. Criar schema inicial do SQLite.
2. Salvar cotacoes normalizadas.
3. Garantir que novas fontes usem o mesmo contrato de persistencia.

## Etapa 5: Expansao de fontes

Objetivo: adicionar novas CEASAs mantendo o mesmo contrato de saida.

Para cada nova fonte:

- Criar coletor proprio.
- Documentar particularidades.
- Garantir que a saida siga o formato padrao.
- Registrar campos que a fonte nao fornece.

## Etapa 6: Qualidade dos dados

Objetivo: melhorar a comparabilidade entre fontes.

Tarefas futuras:

- Normalizar nomes de produtos.
- Padronizar unidades.
- Relatar exemplos nao combinados no complemento PROHORT antes de criar equivalencias.
- Criar equivalencias de produtos entre fontes somente a partir de falhas reais de match.
- Detectar registros duplicados.
- Registrar falhas de coleta.
- Manter historico por data de cotacao.

## Decisoes iniciais

- Python sera a opcao natural para o scraper pela disponibilidade de bibliotecas de coleta, parsing e tratamento de dados.
- A primeira versao deve usar SQLite como arquivo final.
- O banco relacional completo deve vir depois da validacao das primeiras fontes.
- Cada fonte deve ter um coletor isolado para reduzir acoplamento.
