# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Enviar relatorio automatico uma vez ao dia](#enviar-relatorio-automatico-uma-vez-ao-dia) | Reduzir os e-mails do workflow para apenas um relatorio diario consolidado. |
| [Blindar publicacao do pacote](#blindar-publicacao-do-pacote) | Evitar substituir o pacote da release quando a rodada tiver falha relevante. |
| [Desempenho do processamento de raws](#desempenho-do-processamento-de-raws) | Medir e reduzir o custo do processamento, principalmente na extracao de PDFs e nas fontes mais lentas. |
| [Aprimorar sincronizacao incremental com Supabase](#aprimorar-sincronizacao-incremental-com-supabase) | Atualizar no Supabase registros antigos que foram corrigidos no banco local. |
| [Migrar documentacao extensa para GitHub Wiki](#migrar-documentacao-extensa-para-github-wiki) | Reorganizar guias longos na Wiki quando o repositorio puder ficar publico. |

## Enviar relatorio automatico uma vez ao dia

O crawler por workflow pode executar a coleta mais de uma vez ao dia. O envio de
e-mail deve ser ajustado para nao mandar um relatorio a cada rodada.

Comportamento esperado:

1. Manter as coletas agendadas ao longo do dia.
2. Publicar o pacote `ceasa-data-latest.tar.gz` normalmente a cada execucao.
3. Enviar e-mail com relatorio somente uma vez por dia.
4. Evitar e-mails duplicados quando houver execucao manual ou mais de uma janela
   automatica no mesmo dia.
5. Registrar no log do workflow quando o envio for pulado por ja ter ocorrido no
   dia.

Uma forma simples de implementar e criar uma janela diaria especifica para o
email, ou salvar um marcador diario na propria release para saber se o relatorio
ja foi enviado.

## Blindar publicacao do pacote

O workflow atual publica novamente `ceasa-data-latest.tar.gz` ao final da
execucao. Antes de ativar mais paralelismo ou aumentar a frequencia, vale
endurecer as regras de publicacao para evitar substituir um pacote bom por uma
rodada com falha relevante.

Comportamento esperado:

1. detectar no relatorio ou na saida quando houve falha de fonte;
2. diferenciar falha parcial aproveitavel de falha que invalida a rodada;
3. bloquear a publicacao quando o pacote novo nao representar melhoria confiavel;
4. preservar o asset anterior da release quando a publicacao for bloqueada;
5. registrar no log do workflow o motivo da decisao.

Essa pendencia nao impede o crawler atual de funcionar, mas reduz risco
operacional quando a coleta ficar mais frequente.

## Desempenho do processamento de raws

Melhorar o desempenho do processamento de raws, principalmente nas fontes com
grande volume de arquivos e nos parsers de PDF.

O relatorio `data/relatorios/persistencia_20260610_204715_856649.md` mostrou:

- duracao total de `1h53min54s`;
- processamento dos raws: `1h30min34s`, aproximadamente `79,5%` do total;
- persistencia no SQLite: `19min07s`, aproximadamente `16,8%` do total;
- complemento PROHORT: `3min35s`, aproximadamente `3,1%` do total;
- CEASA-PR, CEASA-PE e CEASA-ES concentraram aproximadamente `85%` da
  execucao;
- a CEASA-PR consumiu aproximadamente `50min12s`, principalmente durante o
  processamento de `618` PDFs.

O estudo confirmou que o principal gargalo esta no processamento dos raws. O
parser, principalmente a extracao e interpretacao dos PDFs, provavelmente
representa uma parte relevante desse custo, mas o fluxo deve ser medido por
etapa antes das otimizacoes.

Medicoes necessarias:

- listagem e leitura dos arquivos;
- extracao de texto dos PDFs;
- parser especifico de cada fonte;
- normalizacao e criacao das cotacoes;
- persistencia no SQLite.

Melhorias a avaliar:

- acompanhar as novas metricas de tempo por etapa nos relatorios;
- acompanhar o ganho do salto de raws ja processados sem alteracao;
- acompanhar o ganho do cache de texto extraido de PDFs;
- processar raws independentes em paralelo com limite configuravel;
- reduzir trabalho repetido dentro dos parsers;
- priorizar a investigacao dos PDFs da CEASA-PR;
- ampliar a instrumentacao se alguma etapa continuar sem granularidade suficiente.

Qualquer otimizacao deve preservar os resultados atuais dos parsers, a
proveniencia dos registros e a capacidade de reconstruir o banco a partir dos
raws.

## Aprimorar sincronizacao incremental com Supabase

Atualmente, a sincronizacao incremental envia registros novos e atualiza as
tabelas pequenas de referencia. Porem, ela nao detecta correcoes feitas em
coletas ou cotacoes que ja haviam sido enviadas ao Supabase.

Exemplo: se o preco de uma cotacao antiga for corrigido no SQLite local, a
sincronizacao incremental nao atualiza essa cotacao no Supabase. Atualmente, e
necessario executar a substituicao completa para enviar a correcao.

A sincronizacao incremental deve identificar e atualizar esses registros
antigos sem precisar substituir todo o banco remoto.

## Migrar documentacao extensa para GitHub Wiki

Quando o repositorio puder ficar publico, avaliar a criacao da Wiki do GitHub
para concentrar a documentacao longa do projeto.

Objetivo:

1. Manter no repositorio apenas a documentacao essencial para rodar e manter o
   codigo.
2. Mover guias longos, estudos tecnicos, roadmap e detalhes operacionais para a
   Wiki.
3. Usar o `README.md` como porta de entrada, com links para as paginas da Wiki.

Conteudos candidatos para a Wiki:

- comandos de operacao detalhados;
- fluxo de coleta;
- fontes e limitacoes;
- modelo de dados;
- pacote `ceasa-data-latest.tar.gz`;
- sincronizacao com Supabase;
- estrategias anti-bloqueio;
- decisoes tecnicas com contexto;
- pendencias, roadmap e estudos tecnicos.

Cuidados:

- manter `.env.example` no repositorio, porque acompanha o comportamento real do
  codigo;
- manter no repositorio configuracoes, comandos criticos e decisoes que precisam
  mudar junto com o codigo;
- evitar que a Wiki vire a unica fonte de informacoes necessarias para executar
  o projeto localmente.
