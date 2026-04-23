# What do language models consider canonical?

## Introduction

A paradox: The literary canon is comprised of a finite number of works and authors, yet it cannot be comprehensively enumerated.

Since canonicity is always contested, it is better understood as a continuum than a binary. Some authors and works are more canonical than others. There is a great difference between authors and works that have attained some degree of canonicity and those that have attained none. But there is also a great difference between those that are highly canonical and those whose status is more uncertain.

Scholars may object to this framing on principle, but it is difficult to refute in practice. For example, who would sincerely argue that Bret Harte is as canonical as Toni Morrison, or that "The Luck of Roaring Camp" as canonical as *Beloved*?

Such comparisons are rarely or never made explicitly. The consensus that Harte is "obviously" less canonical than Morrison goes unrecorded. Making implicit views explicit is valuable because then they can be studied and contested. While it would be ideal to ask literary scholars to make hundreds of thousands of such comparisons, it is also prohibitively difficult. As a first step, we can try to simulate them.

## Why?

All proxies for the literary canon have weaknesses. Anthologies structurally disfavor prose, especially the novel. Popular culture that engages with literature, such as *Jeopardy*, provides some insight into the canon, but also falls victim to presentism.

Language models (LMs) have been trained on data legally and illegally scraped from the internet. This provides another such proxy measure for these judgments. While LMs like ChatGPT and Claude are increasingly being used to simulate aspects of culture, they have not yet been used to simulate judgments of canonicity to my knowledge.

## "You are a professor"

I provide an LM (`gpt-5.4-nano`) with the following system prompt:

> You are a professor of American literatures. Which is more canonical?

The model is then presented with a choice, formatted like so:

> 1: Bret Harte
>
> 2: Toni Morrison

Or, in the case of works, like so:

> 1: The Luck of Roaring Camp - Bret Harte
>
> 2: Beloved - Toni Morrison

The LM returns the integer indicating its choice.

This process can be repeated many times using many different comparisons for a trivial cost. 250,000 such comparisons cost about $7. They could run for even less by using local LMs.

### Why "professor of American literatures?"

The hard part of this task is identifying which authors and works to compare, and what the principle of comparison ought to be. It is well established in the research that the school is the place where the literary canon is institutionalized, no where more so than the university. This role for the LM specifies the type of canonicity in question.

Through [previous research](https://fredner.org/literary-studies-according-to-the-mla-international-bibliography.html), I have access to data describing records in *The MLA International Bibliography* (*MLAIB*) pertaining to literatures in English. The authors and works represented in this project include those designated as part of American literature by the bibliographers. Works and authors are also filtered to those that appeared in 20 or more *MLAIB* records to reduce the number of total comparisons required, coresponding to roughly the top 10% of works and authors.

One could object that limiting to roughly the top 10% of authors is already quite exclusionary. Yet it is not difficult to find obscure authors or works with twenty records. I will confess that I had not heard of [Gilbert Imlay](https://en.wikipedia.org/wiki/Gilbert_Imlay) or [Norman Macleod](https://archives.yale.edu/agents/people/88119), though I did know [Mei-mei Berssenbrugge](https://en.wikipedia.org/wiki/Mei-mei_Berssenbrugge) and [Rae Armantrout](https://en.wikipedia.org/wiki/Rae_Armantrout), all of whom have twenty records.

## Evaluation

Because of the nature of the canon described above, it is not possible to say with precision whether the model's judgments are "correct." But there are several measures that can be used to see how they differ from other proxies for the canon.

### *MLAIB*

The *MLAIB* itself implicitly ranks authors and works by indicating the degree of attention they have received from indexed literary scholarship and by the bibliographers who create these records. Big changes in rank between the Elo ranking implied in the relative position of an author or work in the absolute number of articles assigned to them and their status can be explained by two factors. One is time: The more contemporary the writer or work, the less time they have had to be studied, and the fewer times they could have possibly been the subject of an indexed piece.

At the level of the individual author and work, this is recorded in the line charts that show the history of the entity's Elo ranking. The left side represents their implicit Elo ranking based on *MLAIB* records, whereas the right side shows their relative position after simulated comparisons.

### Convergence

Due to the autoregressive properties of LMs, every generated response has a chance of containing a hallucination. In this case, that would require the model to choose the "wrong" integer in its output (e.g., selecting Harte over Morrison).

Over a large number of iterations, Elo scores change less because authors and works have been compared to one another and to similarly ranked entities previously. When this happens, outcomes become less informative, except in cases where an expected outcome is reversed (e.g., selecting "Roaring Camp" over *Beloved*). As the number of matches increases, changes in Elo rankings decrease.