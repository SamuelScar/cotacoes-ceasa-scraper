# Plano de evolução da issue #9

Referência: [Issue #9 — revisar e remodelar a estrutura do banco de dados](https://github.com/SamuelScar/cotacoes-ceasa-scraper/issues/9)

Modelo atual: [modelo-banco-proposto.dbml](modelo-banco-proposto.dbml)

## Objetivo

Remodelar o banco SQLite sem perder histórico ou proveniência e permitir uma migração reversível. O Supabase é considerado legado e não receberá o novo modelo nesta issue.

## Estado atual

- [x] Modelo atual analisado.
- [x] Primeira proposta DBML criada.
- [x] Responsabilidades de coleta e cotação separadas.
- [x] Impactos gerais no crawler documentados na issue.
- [x] Decisões de modelagem necessárias para a etapa 1 concluídas.
- [x] Modelo aprovado para implementação.
- [ ] Migração implementada.

## Decisões vigentes

- Cada tentativa gera uma linha em `coletas`, inclusive falhas e duplicidades.
- SHA-256 identifica somente documentos iguais; cotações não possuem hash próprio.
- Documentos diferentes preservam suas próprias cotações, mesmo com valores iguais.
- Arquivos repetidos são descartados, mas a tentativa permanece registrada.
- O arquivo bruto usa caminho relativo e não possui campos separados para nome ou ETag.
- `cotacoes` referencia diretamente `coleta_id` e `produto_alias_id`.
- Produtos canônicos e textos publicados permanecem separados por `produtos` e `produto_aliases`.
- Categoria, variedade, classificação e procedência podem estar ausentes quando a fonte não as informar com segurança.
- Permanecem apenas `preco_minimo`, `preco_comum` e `preco_maximo`.
- O PROHORT pode preencher um campo vazio uma única vez, registrando a alteração em `cotacao_complementos`.
- Novas cotações do PROHORT entram diretamente em `cotacoes`.
- `fontes.uf` é opcional e cada entreposto possui sua própria UF.
- O controle operacional de coleta histórica fica em `controle_backfill`.
- `schema_migrations` permanece como registro técnico das migrations; tabelas
  operacionais legadas não serão copiadas sem uma função no modelo novo.
- Views e agregações analíticas permanecem fora do escopo da issue.
- O SQLite é o banco ativo e a referência para o novo modelo.
- O Supabase permanece apenas como legado, sem adaptação ou migração nesta issue.

## Decisões substituídas

- `produto_alias_coletas` foi removida; a rastreabilidade é feita diretamente por `cotacoes`.
- `caminho_arquivo_raw` com caminho completo foi substituído por `caminho_relativo_raw`.
- `estados_backfill` foi renomeada para `controle_backfill`.

Os comentários antigos da issue permanecem como histórico, mas o DBML atual representa a proposta vigente.

## Pendências de modelagem

- [x] Definir a duplicidade por `(fonte_id, sha256)`.
- [x] Definir regras seguras para separar produto, variedade e classificação.
- [x] Garantir um único backfill geral no SQLite com índice único parcial quando `categoria_id` for nulo.
- [x] Confirmar quais tabelas operacionais permanecerão no esquema SQLite final.
- [x] Aprovar formalmente o DBML antes de iniciar a implementação.

### Regra para produtos na migração

- Preservar o texto publicado em `produto_aliases.texto_original`.
- Criar inicialmente o produto canônico a partir do nome completo, normalizando
  apenas maiúsculas, minúsculas e espaços.
- Não considerar automaticamente a última palavra como variedade ou classificação.
- Preencher `variedade` somente quando o parser identificar essa estrutura de
  forma explícita; atualmente isso ocorre na CEASA-DF.
- Manter os campos nulos quando a separação não for segura.
- Adiar fusões entre produtos canônicos para depois do inventário dos aliases;
  a migration inicial não tentará adivinhar equivalências.

## Etapas principais

### 1. Adaptar o código e preparar o novo esquema

- [x] Adaptar o modelo `Cotacao`, incluindo `variedade` opcional.
- [x] Adaptar parsers sem inferir variedade ou classificação quando houver dúvida.
- [x] Registrar a coleta antes do download e finalizar seu status ao término do fluxo.
- [x] Calcular o SHA-256 antes do processamento do documento.
- [x] Adaptar o PROHORT para coletas próprias, UF por entreposto e histórico de complementos.
- [x] Adaptar o controle de backfill.
- [x] Implementar o esquema novo no SQLite sem remover o esquema atual.
- [x] Preservar as restrições de preço, chaves estrangeiras e índices definidos no DBML.
- [x] Adicionar testes unitários do esquema v4, duplicidade documental e PROHORT.
- [x] Adaptar consultas operacionais e bloquear claramente manutenções exclusivas do legado.
- [x] Validar em teste isolado o fluxo de download, duplicidade, processamento e persistência em banco v4 vazio.

### 2. Migrar, validar e ativar os dados existentes

- [ ] Registrar contagens, tamanho, integridade e desempenho do banco atual.
- [ ] Auditar os aliases históricos antes de propor fusões de produtos canônicos.
- [ ] Mapear consumidores de `chave_identidade`, `chave_unica` e `cotacao_proveniencias`.
- [ ] Criar uma migration versionada, reiniciável e com checkpoint.
- [ ] Migrar cadastros e aliases.
- [ ] Migrar coletas e arquivos brutos.
- [ ] Migrar cotações preservando documento e texto de origem.
- [ ] Migrar complementos do PROHORT e estado do backfill.
- [ ] Manter campos e tabelas legadas enquanto existirem consumidores dependentes.
- [ ] Comparar contagens, preços, datas, relacionamentos e proveniência.
- [ ] Comparar tamanho e desempenho antes e depois.
- [ ] Atualizar a auditoria para o novo modelo.
- [ ] Definir o ponto de troca entre esquema antigo e novo.
- [ ] Manter backup e esquema anterior até a validação final.
- [ ] Documentar o procedimento de rollback.
- [ ] Remover estruturas legadas somente após aprovação dos resultados.

## Migrations existentes no projeto

O projeto não utiliza Alembic, Flyway ou outra ferramenta geral de migrations.
Atualmente existe um mecanismo próprio composto por:

- `PRAGMA user_version` para versionar o esquema SQLite;
- tabela `schema_migrations` para registrar migrations aplicadas;
- chave única da migration, data, checkpoint e detalhes;
- migrations específicas e idempotentes para proveniência histórica e recuperação da CEASA-DF;
- validações antes e depois da aplicação;
- criação legada do esquema PostgreSQL/Supabase com `CREATE TABLE IF NOT EXISTS`.

A remodelagem da issue #9 deverá reutilizar os conceitos de versão, marcador,
checkpoint e validação, mas precisará de uma migration própria para substituir
a estrutura completa no SQLite.

## Registro de evolução

| Data | Estado | Alteração |
| --- | --- | --- |
| 2026-10-08 | Em planejamento | Proposta DBML criada e decisões principais documentadas na issue. |
| 2026-10-08 | Em revisão | Relacionamentos, campos, ordem semântica e impactos no crawler revisados. |
| 2026-10-08 | Escopo ajustado | SQLite definido como banco ativo; Supabase mantido apenas como legado. |
| 2026-10-08 | Etapa 1 iniciada | Domínio adaptado e esquema SQLite v4 isolado do banco legado. |
| 2026-10-08 | Etapa 1 em implementação | Coletas, duplicidade documental, persistência, backfill, health check e PROHORT integrados ao esquema v4. |
| 2026-10-09 | Revisão estática | Proteções do legado, reposição de raw canônico ausente e testes unitários do v4 adicionados. |
| 2026-10-09 | Etapa 1 validada | Fluxo v4 vazio coberto de ponta a ponta e suíte completa executada no ambiente Docker. |

## Regra de acompanhamento

Ao concluir uma decisão ou etapa:

1. atualizar este checklist;
2. registrar a justificativa na issue #9;
3. atualizar o DBML quando houver mudança estrutural;
4. não remover estruturas legadas antes da validação e aprovação da migração.
