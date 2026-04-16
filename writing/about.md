# What do language models consider canonical?

## Introduction

Let's start with the paradox: The literary canon is comprised of a finite number of works and authors, yet it can never be comprehensively enumerated. No single measure of an author or work's status is adequate to account for their canonicity.

Since canonicity is always being contested, it is better understood as a continuum than a binary. Of course, there is a great difference between authors and works that have attained some degree of canonicity and those that have attained none. But there is also a great difference between those that are highly canonical and those whose status is more marginal.

Scholars already hold such views implicitly. For example, it may be difficult to find a literary scholar who would sincerely make the argument that Bret Harte is more canonical than Chinua Achebe, or that "The Luck of Roaring Camp" is more canonical than *Things Fall Apart*. But this comparison---and millions of other such comparisons---are never made explicitly. The fact that Harte is "obviously" less canonical than Achebe (in the sense that most literature scholars would likely agree with that proposition) goes unrecorded.

We can, however, try to simulate them.

## "You are an English professor"

Language models (LMs) like ChatGPT and Claude are increasingly being used to simulate aspects of culture. However, their ability to simulate the relative status of authors and works in the literary canon in this way has not yet been tested.

I provide an LM (`gpt-5.4-nano`) with the following system prompt:

> You are an English professor. Which is more canonical?

The model is then presented with a choice, formatted like so:

> 1: Bret Harte
> 2: Chinua Achebe

Or, in the case of works, like so:

> 1: The Luck of Roaring Camp - Bret Harte
> 2: Things Fall Apart - Chinua Achebe

The LM returns the integer indicating which it would choose to respond to the question.

This process can be repeated many times using many different comparisons for a trivial cost. 250,000 such comparisons cost about $7, and could run for even less by using local LMs.

### Why "English professor?"

The hard part of this task is identifying which authors and works ought to be compared.

Through a previous research project, I have access to data describing the records in *The MLA International Bibliography* pertaining to literatures in English. The authors and works compared here represent the most frequently referenced in that dataset. I used a cutoff of 50 or more appearances for a given author or work as one of the subjects of an essay, chapter, or monograph. This is a proxy for the canon within the canon: There is a very long tail of authors and works in the *MLAIB* that have infrequently been discussed as primary subjects, and comparing all of them would be impossible. While this inevitably excludes some authors, so too does the object of analysis--canonicity. This filter returned a total of about 2,500 authors and 1,200 works to compare.

The gap between subject authors and subject works reflects a variety of factors. One is formal: In an essay, chapter, or monograph about a poet, rarely does a single poem serve as the primary subject of the entire essay. Subject works generally tend to be longer, and

### Convergence

By running

Due to the autoregressive properties of LMs, every generated response has a chance of containing a hallucination. In this case, that would

### Qualifications
