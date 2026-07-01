# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Enviar relatorio automatico uma vez ao dia](#enviar-relatorio-automatico-uma-vez-ao-dia) | Reduzir os e-mails do workflow para apenas um relatorio diario consolidado. |
| [Criterios de qualidade da rodada](#criterios-de-qualidade-da-rodada) | Definir quando uma rodada com falha parcial deve ser marcada como invalida. |
| [Dificuldades nas coletas recentes](#dificuldades-nas-coletas-recentes) | Investigar fontes com timeouts, historico parcial e falhas de parser/persistencia. |
| [Desempenho do processamento de raws](#desempenho-do-processamento-de-raws) | Medir e reduzir o custo do processamento, principalmente na extracao de PDFs e nas fontes mais lentas. |
| [Avaliar otimizacao dos PDFs brutos](#avaliar-otimizacao-dos-pdfs-brutos) | Testar melhor a reducao de tamanho com qpdf antes de usar no backup oficial. |
| [Aprimorar sincronizacao incremental com Supabase](#aprimorar-sincronizacao-incremental-com-supabase) | Atualizar no Supabase registros antigos que foram corrigidos no banco local. |
| [Migrar documentacao extensa para GitHub Wiki](#migrar-documentacao-extensa-para-github-wiki) | Reorganizar guias longos na Wiki quando o repositorio puder ficar publico. |

## Enviar relatorio automatico uma vez ao dia

O crawler por workflow pode executar a coleta mais de uma vez ao dia. O envio de
e-mail deve ser ajustado para nao mandar um relatorio a cada rodada.

Comportamento esperado:

1. Manter as coletas agendadas ao longo do dia.
2. Publicar `cotacoes.sqlite.xz` normalmente a cada execucao.
3. Enviar e-mail com relatorio somente uma vez por dia.
4. Evitar e-mails duplicados quando houver execucao manual ou mais de uma janela
   automatica no mesmo dia.
5. Registrar no log do workflow quando o envio for pulado por ja ter ocorrido no
   dia.

Uma forma simples de implementar e criar uma janela diaria especifica para o
email, ou salvar um marcador diario na propria release para saber se o relatorio
ja foi enviado.

## Criterios de qualidade da rodada

O workflow ja tenta salvar o que foi processado mesmo quando uma etapa posterior
falha: o pacote completo vai para o OneDrive, o banco compactado vai para a release
e a validacao final falha somente depois das tentativas de salvamento.

Ainda falta definir criterios de qualidade para decidir quando uma rodada com
falha parcial deve ser considerada invalida para consumo.

Comportamento esperado:

1. detectar no relatorio ou na saida quando houve falha de fonte;
2. diferenciar falha parcial aproveitavel de falha que invalida a rodada;
3. registrar no log do workflow se a rodada e valida, parcial ou invalida;
4. enviar essa classificacao no relatorio.

## Dificuldades nas coletas recentes

A analise dos relatorios em `data/relatorios/` ate `2026-06-28` mostrou que o
workflow esta estavel, mas ainda conclui com avisos recorrentes. Nao ha
indicacao de bloqueio explicito por `403`, `429`, `HttpSourceBlocked`,
`Forbidden` ou `Too Many Requests`; os problemas observados parecem estar mais
ligados a timeout, fonte instavel, limite de busca historica e parser.

Pontos a investigar com mais cuidado:

1. CEASA-PR baixa e processa muitos raws, mas a persistencia pode falhar com
   `ValueError: Cotacao sem data`. No relatorio
   `download_e_persistencia_20260628_190209_398039.md`, a fonte processou
   `251` de `254` raws e extraiu `54.225` cotacoes, mas a persistencia da fonte
   falhou.
2. CEASA-PR tambem tem PDFs invalidos ou ausentes recorrentes, principalmente
   `Invalid Elementary Object`, `Stream has ended unexpectedly` e PDFs nao
   encontrados para algumas datas de Maringa, Londrina e Foz do Iguacu.
3. CEASA-Campinas e CEASA-RJ aparecem com timeouts frequentes e, quando o
   download falha, a persistencia fica como `persistencia ignorada porque o
   download falhou`.
4. CEASA-GO e CEASA-CE tambem apresentam timeouts, mas com frequencia menor que
   Campinas e RJ nas coletas recentes.
5. CEASA-BA e CEAGESP-SP frequentemente nao encontram todas as datas pedidas na
   janela historica (`51 datas de cotacao apos 204 tentativas`), mas ainda
   aproveitam os arquivos baixados antes da falha.
6. A publicacao do pacote deve considerar essas falhas parciais para evitar
   substituir um pacote bom por uma rodada com perda relevante de fontes.

Acoes futuras:

- corrigir primeiro a persistencia da CEASA-PR, porque ela concentra o maior
  volume potencial perdido;
- separar falha de download, falha de parser e falha de persistencia no resumo
  para deixar o impacto por fonte mais claro;
- avaliar retries/backoff ou coleta separada para CEASA-Campinas e CEASA-RJ;
- avaliar uma politica especifica para fontes com historico parcial, como BA e
  CEAGESP-SP;
- acompanhar nos proximos relatorios se surgem sinais reais de bloqueio HTTP
  (`403`, `429` ou `Retry-After`).

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

- comparar por rodada `COTACOES_WORKERS`, tempo total do pipeline, janela de
  downloads, tempo acumulado de persistencia, espera na fila e backlog maximo;
- listagem e leitura dos arquivos;
- extracao de texto dos PDFs;
- parser especifico de cada fonte;
- normalizacao e criacao das cotacoes;
- persistencia no SQLite.

Melhorias a avaliar:

- acompanhar as novas metricas de tempo por etapa nos relatorios;
- comparar duracao, volume baixado e `COTACOES_WORKERS` para confirmar o
  ganho real do paralelismo entre fontes;
- acompanhar o ganho do salto de raws ja processados sem alteracao;
- acompanhar o ganho do cache de texto extraido de PDFs;
- processar raws independentes em paralelo com limite configuravel;
- reduzir trabalho repetido dentro dos parsers;
- priorizar a investigacao dos PDFs da CEASA-PR;
- ampliar a instrumentacao se alguma etapa continuar sem granularidade suficiente.

Qualquer otimizacao deve preservar os resultados atuais dos parsers, a
proveniencia dos registros e a capacidade de reconstruir o banco a partir dos
raws.

## Avaliar otimizacao dos PDFs brutos

Foram feitos testes locais usando `qpdf` para reduzir o tamanho dos PDFs brutos
sem alterar o conteudo aparente dos arquivos. A ideia pode ajudar a diminuir o
tamanho do backup completo, principalmente no OneDrive, mas ainda nao deve entrar
no fluxo oficial.

Motivo da cautela:

1. alguns PDFs das fontes ja chegam danificados ou fora do padrao esperado;
2. o `qpdf` consegue emitir avisos e reconstruir parcialmente alguns arquivos,
   mas isso precisa ser avaliado com cuidado;
3. ainda falta confirmar que o PDF otimizado nao interfere na extracao de texto,
   na interpretacao dos parsers e na persistencia correta no SQLite;
4. qualquer ganho de espaco nao pode comprometer a capacidade de reconstruir o
   banco a partir dos raws.

Decisao atual:

- manter o otimizador de PDF apenas como experimento local;
- nao incluir a otimizacao de PDFs no workflow oficial por enquanto;
- continuar preservando os raws originais no backup completo;
- usar `xz` no empacotamento oficial, porque ja reduz o tamanho do backup sem
  mexer no conteudo interno dos PDFs.

Antes de considerar essa otimizacao como parte do produto, validar pelo menos:

- tamanho antes/depois por fonte;
- quantidade de PDFs com aviso ou erro do `qpdf`;
- comparacao da extracao de texto antes/depois;
- comparacao das cotacoes persistidas antes/depois;
- impacto no tempo total do workflow.

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
- banco `cotacoes.sqlite.xz`;
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
